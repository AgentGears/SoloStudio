from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from solostudio.app.bootstrap import bootstrap
from solostudio.kernel.clock import FixedClock
from solostudio.kernel.ids import SequenceIdSource


class KernelIdentitySpineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.kernel = bootstrap(self.data_dir, clock=FixedClock(), ids=SequenceIdSource())
        self.project_id = self.kernel.productions.create_project("M0", "project-key")
        self.production_id = self.kernel.productions.create_production(self.project_id, "Explainer", "production-key")

    def tearDown(self) -> None:
        self.kernel.close()
        self.temp_dir.cleanup()

    def command(self, version: int, key: str, action: str, data: dict, channel="user"):
        return getattr(self.kernel, channel).command(
            production_id=self.production_id,
            expected_state_version=version,
            idempotency_key=key,
            action=action,
            command_input=data,
        )

    def capture(self, version: int, key: str):
        return self.kernel.user.capture_revision(
            production_id=self.production_id,
            expected_state_version=version,
            idempotency_key=key,
        )

    def test_creation_keys_are_idempotent(self) -> None:
        self.assertEqual(self.project_id, self.kernel.productions.create_project("ignored", "project-key"))
        self.assertEqual(self.production_id, self.kernel.productions.create_production(self.project_id, "ignored", "production-key"))

    def test_stale_command_cannot_overwrite_newer_state(self) -> None:
        first = self.command(0, "ui-1", "set_script", {"text": "newer"})
        stale = self.command(0, "agent-1", "set_script", {"text": "older"}, "agent")
        state = self.kernel.productions.working_state(self.production_id)
        self.assertEqual(first.classification, "COMMITTED")
        self.assertEqual(stale.classification, "STALE_COMMAND")
        self.assertEqual(state["state_version"], 1)
        self.assertEqual(state["state"]["script"]["text"], "newer")

    def test_lost_response_retry_replays_before_stale_check(self) -> None:
        committed = self.command(0, "same-key", "set_script", {"text": "A"})
        replayed = self.command(0, "same-key", "set_script", {"text": "A"})
        state = self.kernel.productions.working_state(self.production_id)
        self.assertEqual(committed.classification, "COMMITTED")
        self.assertEqual(replayed.classification, "REPLAYED")
        self.assertEqual(replayed.receipt_id, committed.receipt_id)
        self.assertEqual(state["state_version"], 1)

    def test_same_key_different_semantics_conflicts(self) -> None:
        self.command(0, "same-key", "set_script", {"text": "A"})
        conflict = self.command(0, "same-key", "set_script", {"text": "B"})
        self.assertEqual(conflict.classification, "IDEMPOTENCY_CONFLICT")
        self.assertEqual(self.kernel.productions.working_state(self.production_id)["state"]["script"]["text"], "A")

    def test_stale_receipt_burns_idempotency_key(self) -> None:
        self.command(0, "advance", "set_script", {"text": "A"})
        stale = self.command(0, "burned", "set_script", {"text": "B"})
        replay = self.command(1, "burned", "set_script", {"text": "B"})
        self.assertEqual(stale.classification, "STALE_COMMAND")
        self.assertEqual(replay.classification, "REPLAYED")
        self.assertEqual(replay.status, "REJECTED_STALE")
        self.assertEqual(self.kernel.productions.working_state(self.production_id)["state_version"], 1)

    def test_agent_and_user_have_distinct_command_fingerprints(self) -> None:
        self.command(0, "principal-key", "set_script", {"text": "A"}, "user")
        conflict = self.command(0, "principal-key", "set_script", {"text": "A"}, "agent")
        self.assertEqual(conflict.classification, "IDEMPOTENCY_CONFLICT")

    def test_actor_like_payload_cannot_spoof_channel_principal(self) -> None:
        result = self.command(0, "spoof", "set_script", {"text": "A", "actor": {"type": "USER"}}, "agent")
        with self.kernel.store.read() as db:
            receipt = db.execute("SELECT principal_type, principal_id FROM production_command_receipts WHERE id = ?", (result.receipt_id,)).fetchone()
        self.assertEqual(receipt["principal_type"], "AGENT")
        self.assertEqual(receipt["principal_id"], "agent-runtime")

    def test_a_to_b_to_a_preserves_three_historical_occurrences(self) -> None:
        self.command(0, "a1", "set_script", {"text": "A"})
        r1 = self.capture(1, "c1")
        canonical_r1 = self.kernel.productions.revision(r1.revision_id)["canonical_json"]

        self.command(1, "b1", "set_script", {"text": "B"})
        r2 = self.capture(2, "c2")

        self.command(2, "a2", "set_script", {"text": "A"})
        r3 = self.capture(3, "c3")
        canonical_r1_after = self.kernel.productions.revision(r1.revision_id)["canonical_json"]
        r3_row = self.kernel.productions.revision(r3.revision_id)

        self.assertEqual([r1.sequence, r2.sequence, r3.sequence], [1, 2, 3])
        self.assertEqual(r1.content_hash, r3.content_hash)
        self.assertNotEqual(r1.revision_id, r3.revision_id)
        self.assertEqual(r3_row["parent_revision_id"], r2.revision_id)
        self.assertEqual(canonical_r1_after, canonical_r1)

    def test_consecutive_no_change_capture_reuses_head(self) -> None:
        self.command(0, "a1", "set_script", {"text": "A"})
        first = self.capture(1, "c1")
        second = self.capture(1, "c2")
        self.assertEqual(second.classification, "NO_CHANGE")
        self.assertEqual(second.revision_id, first.revision_id)
        self.assertEqual(len(self.kernel.productions.revisions(self.production_id)), 1)

    def test_capture_and_edit_are_coherent_under_writer_serialization(self) -> None:
        self.command(0, "seed", "set_script", {"text": "A"})
        barrier = threading.Barrier(2)
        results: list[object] = []

        def capture_worker() -> None:
            barrier.wait()
            results.append(self.capture(1, "capture-race"))

        def edit_worker() -> None:
            barrier.wait()
            results.append(self.command(1, "edit-race", "set_script", {"text": "B"}))

        t1 = threading.Thread(target=capture_worker)
        t2 = threading.Thread(target=edit_worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        revisions = self.kernel.productions.revisions(self.production_id)
        self.assertLessEqual(len(revisions), 1)
        if revisions:
            captured = json.loads(revisions[0]["canonical_json"])
            self.assertEqual(captured["script"]["text"], "A")
        state = self.kernel.productions.working_state(self.production_id)
        self.assertEqual(state["state"]["script"]["text"], "B")
        self.assertEqual(state["state_version"], 2)

    def test_restart_preserves_state_and_history(self) -> None:
        self.command(0, "a1", "set_script", {"text": "A"})
        captured = self.capture(1, "c1")
        self.kernel.close()
        self.kernel = bootstrap(self.data_dir, clock=FixedClock(), ids=SequenceIdSource())
        state = self.kernel.productions.working_state(self.production_id)
        revision = self.kernel.productions.revision(captured.revision_id)
        self.assertEqual(state["state_version"], 1)
        self.assertEqual(json.loads(revision["canonical_json"])["script"]["text"], "A")


if __name__ == "__main__":
    unittest.main()
