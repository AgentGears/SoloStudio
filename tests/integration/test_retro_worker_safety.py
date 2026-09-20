from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from solostudio.app.bootstrap import bootstrap
from solostudio.kernel.clock import FixedClock
from solostudio.kernel.costs import CostPlan
from solostudio.kernel.ids import SequenceIdSource


class RetroWorkerSafetyTests(unittest.TestCase):
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

    def artifact_job(
        self,
        fingerprint: str,
        *,
        with_cost: bool = False,
        billing_ambiguous_on_interrupt: bool = False,
    ):
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=0,
            idempotency_key=f"script-{fingerprint}",
            action="set_script",
            command_input={"text": "seed"},
        )
        revision = self.kernel.user.capture_revision(
            production_id=self.production_id,
            expected_state_version=1,
            idempotency_key=f"capture-{fingerprint}",
        )
        return self.kernel.jobs.admit(
            production_id=self.production_id,
            job_class="ARTIFACT",
            job_type="VOICE_SYNTHESIZE",
            semantic_capability="voice.synthesize",
            spec={"output": "voice.bin"},
            route={
                "executor": "placeholder",
                "billing_ambiguity_on_interrupt": billing_ambiguous_on_interrupt,
            },
            input_fingerprint=fingerprint,
            production_revision_id=revision.revision_id,
            max_attempts=1,
            cost_plan=CostPlan("voice.synthesize", 1, 1) if with_cost else None,
        )

    def test_artifact_completion_uses_authoritative_job_fingerprint(self) -> None:
        admission = self.artifact_job("authoritative-fingerprint")
        temp_dir = self.kernel.jobs.start_attempt(admission.attempt_id, "test-worker")
        (temp_dir / "voice.bin").write_bytes(b"voice-bytes")
        artifact_ids = self.kernel.jobs.complete_artifact_attempt(
            admission.attempt_id,
            [{
                "role": "primary",
                "path": "voice.bin",
                "kind": "source_reference",
                "media_type": "application/octet-stream",
                "producer_stage": "test",
            }],
        )
        artifact = self.kernel.artifacts.artifact(artifact_ids[0])
        self.assertEqual(artifact["input_fingerprint"], "authoritative-fingerprint")
        self.assertTrue(self.kernel.artifacts.is_current_for(artifact_ids[0], "authoritative-fingerprint"))

    def test_costed_artifact_success_settles_reservation(self) -> None:
        admission = self.artifact_job("costed-success", with_cost=True)
        self.assertEqual(self.kernel.costs.for_job(admission.job_id)["state"], "RESERVED")
        temp_dir = self.kernel.jobs.start_attempt(admission.attempt_id, "test-worker")
        (temp_dir / "voice.bin").write_bytes(b"voice-bytes")
        self.kernel.jobs.complete_artifact_attempt(
            admission.attempt_id,
            [{
                "role": "primary",
                "path": "voice.bin",
                "kind": "source_reference",
                "media_type": "application/octet-stream",
                "producer_stage": "test",
            }],
        )
        cost = self.kernel.costs.for_job(admission.job_id)
        self.assertEqual(cost["state"], "SETTLED")
        self.assertEqual(cost["settled_microunits"], 1)

    def test_non_list_artifact_outputs_are_rejected_with_domain_error(self) -> None:
        admission = self.artifact_job("non-list-output")
        self.kernel.jobs.start_attempt(admission.attempt_id, "test-worker")
        from solostudio.kernel.errors import InvalidArtifact
        with self.assertRaises(InvalidArtifact):
            self.kernel.jobs.complete_artifact_attempt(admission.attempt_id, None)  # type: ignore[arg-type]
        self.assertEqual(self.kernel.jobs.attempt(admission.attempt_id)["state"], "RUNNING")

    def test_non_object_worker_output_fails_instead_of_wedging_job(self) -> None:
        admission = self.artifact_job("bad-output")
        result = {"status": "SUCCEEDED", "outputs": [None]}
        code = "import json; json.dump(" + repr(result) + ", open('result.json','w',encoding='utf-8'))"
        run = self.kernel.worker.run(admission.attempt_id, [sys.executable, "-c", code])
        self.assertEqual(run.attempt_state, "FAILED")
        self.assertEqual(run.job_state, "FAILED")
        self.assertEqual(self.kernel.jobs.attempt(admission.attempt_id)["error_code"], "INVALID_ARTIFACT")

    def test_missing_worker_executable_fails_attempt_and_job(self) -> None:
        admission = self.artifact_job("spawn-failure", with_cost=True)
        run = self.kernel.worker.run(admission.attempt_id, [str(self.root / "missing-executable")])
        self.assertEqual(run.attempt_state, "FAILED")
        self.assertEqual(run.job_state, "FAILED")
        self.assertEqual(self.kernel.jobs.attempt(admission.attempt_id)["error_code"], "WORKER_START_FAILED")
        self.assertEqual(self.kernel.costs.for_job(admission.job_id)["state"], "RELEASED")

    def test_request_staging_oserror_fails_attempt_and_job(self) -> None:
        admission = self.artifact_job("request-write-failure", with_cost=True)
        original_write_text = Path.write_text

        def failing_write_text(path: Path, *args, **kwargs):
            if path.name == "request.json":
                raise OSError("simulated staging failure")
            return original_write_text(path, *args, **kwargs)

        with patch.object(Path, "write_text", failing_write_text):
            run = self.kernel.worker.run(admission.attempt_id, [sys.executable, "-c", "pass"])
        self.assertEqual(run.attempt_state, "FAILED")
        self.assertEqual(run.job_state, "FAILED")
        self.assertEqual(self.kernel.jobs.attempt(admission.attempt_id)["error_code"], "WORKER_START_FAILED")
        self.assertEqual(self.kernel.costs.for_job(admission.job_id)["state"], "RELEASED")

    def test_ambiguous_worker_timeout_marks_cost_unknown(self) -> None:
        admission = self.artifact_job(
            "ambiguous-timeout",
            with_cost=True,
            billing_ambiguous_on_interrupt=True,
        )
        run = self.kernel.worker.run(
            admission.attempt_id,
            [sys.executable, "-c", "import time; time.sleep(0.5)"],
            timeout_seconds=0.05,
        )
        self.assertEqual(run.attempt_state, "FAILED")
        self.assertEqual(run.job_state, "FAILED")
        self.assertEqual(self.kernel.jobs.attempt(admission.attempt_id)["error_code"], "WORKER_TIMEOUT")
        self.assertEqual(self.kernel.costs.for_job(admission.job_id)["state"], "UNKNOWN")

    def test_signal_interrupted_worker_marks_cost_unknown_when_route_is_ambiguous(self) -> None:
        admission = self.artifact_job(
            "ambiguous-signal",
            with_cost=True,
            billing_ambiguous_on_interrupt=True,
        )
        completed = subprocess.CompletedProcess(["worker"], -15, "", "terminated")
        with patch("solostudio.kernel.jobs.worker.subprocess.run", return_value=completed):
            run = self.kernel.worker.run(admission.attempt_id, ["worker"])
        self.assertEqual(run.exit_code, -15)
        self.assertEqual(run.attempt_state, "FAILED")
        self.assertEqual(run.job_state, "FAILED")
        self.assertEqual(self.kernel.jobs.attempt(admission.attempt_id)["error_code"], "WORKER_PROCESS_FAILED")
        self.assertEqual(self.kernel.costs.for_job(admission.job_id)["state"], "UNKNOWN")

    def test_orphan_attempt_namespace_is_cleaned_before_first_committed_start(self) -> None:
        admission = self.artifact_job("orphan-namespace")
        attempt = self.kernel.jobs.attempt(admission.attempt_id)
        path = self.root / attempt["temp_relpath"]
        path.mkdir(parents=True)
        (path / "stale-partial.txt").write_text("stale", encoding="utf-8")

        started = self.kernel.jobs.start_attempt(admission.attempt_id, "test-worker")
        self.assertEqual(started, path)
        self.assertFalse((path / "stale-partial.txt").exists())
        self.assertEqual(self.kernel.jobs.attempt(admission.attempt_id)["state"], "RUNNING")


if __name__ == "__main__":
    unittest.main()
