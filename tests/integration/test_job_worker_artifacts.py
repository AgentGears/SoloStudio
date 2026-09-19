from __future__ import annotations

import sys

from tests.integration.job_test_support import JobTestCase


class JobWorkerArtifactTests(JobTestCase):
    def test_exit_zero_without_worker_result_fails_and_registers_no_artifact(self) -> None:
        revision = self.capture_revision()
        admission = self.kernel.jobs.admit(
            production_id=self.production_id,
            job_class="ARTIFACT",
            job_type="VOICE_SYNTHESIZE",
            semantic_capability="voice.synthesize",
            spec={"output": "voice.bin"},
            route={"executor": "placeholder"},
            input_fingerprint="voice-fp",
            production_revision_id=revision.revision_id,
            max_attempts=1,
        )
        run = self.kernel.worker.run(admission.attempt_id, [sys.executable, "-c", "pass"])
        self.assertEqual(run.attempt_state, "FAILED")
        self.assertEqual(run.job_state, "FAILED")
        self.assertEqual(self.kernel.jobs.job(admission.job_id)["state"], "FAILED")
        with self.kernel.store.read() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM artifacts WHERE producer_job_id = ?", (admission.job_id,)).fetchone()[0],
                0,
            )

    def test_artifact_job_succeeds_only_after_registered_output(self) -> None:
        revision = self.capture_revision()
        admission = self.kernel.jobs.admit(
            production_id=self.production_id,
            job_class="ARTIFACT",
            job_type="VOICE_SYNTHESIZE",
            semantic_capability="voice.synthesize",
            spec={"output": "voice.bin"},
            route={"executor": "placeholder"},
            input_fingerprint="voice-success-fp",
            production_revision_id=revision.revision_id,
            max_attempts=1,
        )
        result = {
            "status": "SUCCEEDED",
            "outputs": [{
                "role": "primary",
                "path": "voice.bin",
                "kind": "source_reference",
                "media_type": "application/octet-stream",
                "producer_stage": "media_worker",
            }],
        }
        code = (
            "from pathlib import Path; import json; "
            "Path('voice.bin').write_bytes(b'voice-bytes'); "
            "json.dump(" + repr(result) + ", open('result.json','w',encoding='utf-8'))"
        )
        run = self.kernel.worker.run(admission.attempt_id, [sys.executable, "-c", code])
        self.assertEqual(run.attempt_state, "SUCCEEDED")
        self.assertEqual(run.job_state, "SUCCEEDED")
        attempt = self.kernel.jobs.attempt(admission.attempt_id)
        artifact_id = attempt["result"]["artifact_ids"][0]
        artifact = self.kernel.artifacts.artifact(artifact_id, verify_bytes=True)
        self.assertEqual(artifact["producer_job_id"], admission.job_id)
        self.assertEqual(artifact["producer_attempt_id"], admission.attempt_id)
        self.assertEqual(self.kernel.artifacts.read_bytes(artifact_id), b"voice-bytes")
