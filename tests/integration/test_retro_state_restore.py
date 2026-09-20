from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from solostudio.app.bootstrap import bootstrap
from solostudio.kernel.backup import restore_backup
from solostudio.kernel.clock import FixedClock
from solostudio.kernel.ids import SequenceIdSource


class RetroStateRestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.ids = SequenceIdSource()
        self.kernel = bootstrap(self.root, clock=FixedClock(), ids=self.ids)
        project_id = self.kernel.productions.create_project("M0", "project")
        self.production_id = self.kernel.productions.create_production(project_id, "Explainer", "production")

    def tearDown(self) -> None:
        try:
            self.kernel.close()
        except Exception:
            pass
        self.temp_dir.cleanup()

    def command(self, version: int, key: str, action: str, data: dict):
        return self.kernel.user.command(
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

    def captured_revision(self):
        self.command(0, "script", "set_script", {"text": "seed"})
        return self.capture(1, "capture")

    def test_boolean_duration_is_rejected_as_invalid_command(self) -> None:
        result = self.command(0, "bool-duration", "update_brief", {"brief": {"duration_min_ms": True}})
        self.assertEqual(result.classification, "REJECTED_INVALID")
        self.assertEqual(self.kernel.productions.working_state(self.production_id)["state_version"], 0)

    def test_selected_variant_ids_do_not_change_revision_identity(self) -> None:
        self.command(0, "script-a", "set_script", {"text": "A"})
        first = self.capture(1, "capture-a")
        selected = self.command(1, "select-v1", "select_variant", {"variant_id": "variant-1"})
        self.assertEqual(selected.classification, "COMMITTED")
        second = self.capture(2, "capture-after-selection")
        self.assertEqual(second.classification, "NO_CHANGE")
        self.assertEqual(second.revision_id, first.revision_id)
        self.assertEqual(len(self.kernel.productions.revisions(self.production_id)), 1)

    def test_restore_removes_legacy_database_layout_before_bootstrap(self) -> None:
        revision = self.captured_revision()
        backup = self.kernel.backups.create_backup()
        self.kernel.close()
        legacy_db = self.root / "studio.db"
        shutil.copy2(backup.path / "db" / "studio.db", legacy_db)
        (self.root / "studio.db-wal").write_bytes(b"stale-wal")
        (self.root / "studio.db-shm").write_bytes(b"stale-shm")

        restore_backup(backup.path, self.root)
        self.assertFalse(legacy_db.exists())
        self.assertFalse((self.root / "studio.db-wal").exists())
        self.assertFalse((self.root / "studio.db-shm").exists())

        self.kernel = bootstrap(self.root, clock=FixedClock(), ids=self.ids)
        self.assertEqual(self.kernel.productions.revision(revision.revision_id)["id"], revision.revision_id)


    def test_same_kind_derivative_does_not_satisfy_capture_source_repair(self) -> None:
        revision = self.captured_revision()
        with self.kernel.store.write() as db:
            db.execute(
                "DELETE FROM artifacts WHERE production_revision_id = ? AND producer_stage = 'revision_capture'",
                (revision.revision_id,),
            )

        derivative_id = self.kernel.artifacts.promote_and_register(
            b"wrong derivative bytes",
            production_id=self.production_id,
            production_revision_id=revision.revision_id,
            kind="script_text",
            media_type="text/plain; charset=utf-8",
            producer_stage="media_worker",
        )

        replay = self.capture(1, "capture-backfill-over-derivative")
        self.assertEqual(replay.classification, "NO_CHANGE")
        artifacts = self.kernel.artifacts.revision_artifacts(revision.revision_id, verify_bytes=True)
        matching = [
            item
            for item in artifacts
            if item["kind"] == "script_text" and item["producer_stage"] == "revision_capture"
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(self.kernel.artifacts.read_bytes(matching[0]["id"]), b"seed")
        self.assertEqual(self.kernel.artifacts.read_bytes(derivative_id), b"wrong derivative bytes")

    def test_no_change_capture_backfills_missing_source_artifacts(self) -> None:
        revision = self.captured_revision()
        with self.kernel.store.write() as db:
            db.execute("DELETE FROM artifact_dependencies")
            db.execute("DELETE FROM artifacts WHERE production_revision_id = ?", (revision.revision_id,))
            db.execute("DELETE FROM objects")
        self.assertEqual(self.kernel.artifacts.revision_artifacts(revision.revision_id), [])

        replay = self.capture(1, "capture-backfill")
        self.assertEqual(replay.classification, "NO_CHANGE")
        self.assertEqual(replay.revision_id, revision.revision_id)
        artifacts = self.kernel.artifacts.revision_artifacts(revision.revision_id, verify_bytes=True)
        self.assertEqual({item["kind"] for item in artifacts}, {"script_text"})
        self.assertEqual(len(self.kernel.productions.revisions(self.production_id)), 1)


if __name__ == "__main__":
    unittest.main()
