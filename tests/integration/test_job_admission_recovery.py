from __future__ import annotations

from solostudio.app.bootstrap import bootstrap
from solostudio.kernel.clock import FixedClock
from tests.integration.job_test_support import JobTestCase


class JobAdmissionRecoveryTests(JobTestCase):
    def test_admission_persists_spec_and_dedupes_active_equivalent(self) -> None:
        admission = self.kernel.jobs.admit(
            production_id=self.production_id,
            job_class="STATE_PROPOSAL",
            job_type="SCRIPT_GENERATE",
            semantic_capability="script.generate",
            spec={"prompt": "draft"},
            route={"executor": "placeholder"},
            input_fingerprint="fp-1",
        )
        duplicate = self.kernel.jobs.admit(
            production_id=self.production_id,
            job_class="STATE_PROPOSAL",
            job_type="SCRIPT_GENERATE",
            semantic_capability="script.generate",
            spec={"prompt": "draft"},
            route={"executor": "placeholder"},
            input_fingerprint="fp-1",
        )
        job = self.kernel.jobs.job(admission.job_id)
        self.assertFalse(admission.reused)
        self.assertTrue(duplicate.reused)
        self.assertEqual(duplicate.job_id, admission.job_id)
        self.assertEqual(job["source_state_version"], 0)
        self.assertEqual(job["spec"], {"prompt": "draft"})
        self.assertEqual(job["route"], {"executor": "placeholder"})
        self.assertEqual(job["state"], "QUEUED")

    def test_restart_interrupts_running_attempt_and_uses_fresh_namespace(self) -> None:
        admission = self.kernel.jobs.admit(
            production_id=self.production_id,
            job_class="STATE_PROPOSAL",
            job_type="SCRIPT_GENERATE",
            semantic_capability="script.generate",
            spec={"prompt": "draft"},
            route={"executor": "placeholder"},
            input_fingerprint="retry-fp",
            max_attempts=2,
        )
        first_path = self.kernel.jobs.start_attempt(admission.attempt_id, "test-worker")
        (first_path / "partial.txt").write_text("partial", encoding="utf-8")
        self.kernel.close()
        self.kernel = bootstrap(self.root, clock=FixedClock(), ids=self.ids)

        attempts = self.kernel.jobs.attempts(admission.job_id)
        self.assertEqual([attempt["state"] for attempt in attempts], ["INTERRUPTED", "CREATED"])
        self.assertNotEqual(attempts[0]["temp_relpath"], attempts[1]["temp_relpath"])
        second_path = self.kernel.jobs.start_attempt(attempts[1]["id"], "test-worker")
        self.assertFalse((second_path / "partial.txt").exists())
        self.assertTrue((first_path / "partial.txt").exists())
