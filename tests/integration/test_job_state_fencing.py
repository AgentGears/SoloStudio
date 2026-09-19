from __future__ import annotations

import sys

from tests.integration.job_test_support import JobTestCase


class JobStateFencingTests(JobTestCase):
    def test_late_state_proposal_is_retained_but_adoption_is_stale(self) -> None:
        admission = self.kernel.jobs.admit(
            production_id=self.production_id,
            job_class="STATE_PROPOSAL",
            job_type="SCRIPT_GENERATE",
            semantic_capability="script.generate",
            spec={"prompt": "generate"},
            route={"executor": "placeholder"},
            input_fingerprint="proposal-fp",
        )
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=0,
            idempotency_key="producer-edit",
            action="set_script",
            command_input={"text": "producer version"},
        )
        proposal = {"status": "SUCCEEDED", "proposal": {"action": "set_script", "input": {"text": "generated version"}}}
        code = "import json; json.dump(" + repr(proposal) + ", open('result.json','w',encoding='utf-8'))"
        run = self.kernel.worker.run(admission.attempt_id, [sys.executable, "-c", code])
        self.assertEqual(run.job_state, "SUCCEEDED")

        adopted = self.kernel.jobs.adopt_state_proposal(admission.job_id, self.kernel.user.principal, "adopt-late")
        self.assertEqual(adopted.classification, "STALE_COMMAND")
        state = self.kernel.productions.working_state(self.production_id)
        self.assertEqual(state["state_version"], 1)
        self.assertEqual(state["state"]["script"]["text"], "producer version")
        attempts = self.kernel.jobs.attempts(admission.job_id)
        self.assertEqual(attempts[0]["state"], "SUCCEEDED")
        self.assertEqual(attempts[0]["result"]["proposal"]["input"]["text"], "generated version")
