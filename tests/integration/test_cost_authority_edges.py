from __future__ import annotations

from solostudio.kernel.costs import CostPlan
from solostudio.kernel.errors import InvalidCostState
from tests.integration.job_test_support import JobTestCase


class CostAuthorityEdgeTests(JobTestCase):
    def test_settlement_cannot_exceed_reserved_authority(self) -> None:
        admission = self.kernel.jobs.admit(
            production_id=self.production_id,
            job_class="STATE_PROPOSAL",
            job_type="SCRIPT_GENERATE",
            semantic_capability="text.generate",
            spec={"projection": {}},
            route={"billing_ambiguity_on_interrupt": False},
            input_fingerprint="settlement-ceiling",
            cost_plan=CostPlan("text.generate", 1, 1),
        )

        with self.kernel.store.write() as db:
            with self.assertRaises(InvalidCostState):
                self.kernel.costs.settle_job_in_tx(db, admission.job_id, 2)

        cost = self.kernel.costs.for_job(admission.job_id)
        self.assertEqual(cost["state"], "RESERVED")
        self.assertIsNone(cost["settled_microunits"])
