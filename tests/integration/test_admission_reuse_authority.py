from __future__ import annotations

from solostudio.kernel.costs import CostPlan
from solostudio.kernel.errors import InvalidCommand
from tests.integration.job_test_support import JobTestCase


class AdmissionReuseAuthorityTests(JobTestCase):
    def test_active_reuse_requires_matching_cost_contract(self) -> None:
        common = {
            "production_id": self.production_id,
            "job_class": "STATE_PROPOSAL",
            "job_type": "SCRIPT_GENERATE",
            "semantic_capability": "text.generate",
            "spec": {"execution_mode": "PRIVATE", "projection": {}},
            "route": {"provider": "local", "execution_mode": "PRIVATE"},
            "input_fingerprint": "same-fingerprint",
        }
        no_cost = self.kernel.jobs.admit(**common)
        reserved = self.kernel.jobs.admit(**common, cost_plan=CostPlan("text.generate", 0, 0))
        self.assertFalse(no_cost.reused)
        self.assertFalse(reserved.reused)
        self.assertNotEqual(no_cost.job_id, reserved.job_id)
        self.assertIsNone(no_cost.cost_id)
        self.assertIsNotNone(reserved.cost_id)

        matching = self.kernel.jobs.admit(**common, cost_plan=CostPlan("text.generate", 0, 0))
        self.assertTrue(matching.reused)
        self.assertEqual(matching.job_id, reserved.job_id)
        self.assertEqual(matching.cost_id, reserved.cost_id)

        different_reservation = self.kernel.jobs.admit(
            **common,
            cost_plan=CostPlan("text.generate", 0, 1),
        )
        self.assertFalse(different_reservation.reused)
        self.assertNotEqual(different_reservation.job_id, reserved.job_id)
        self.assertEqual(self.kernel.costs.for_job(different_reservation.job_id)["reserved_microunits"], 1)

    def test_active_reuse_requires_matching_retry_limit(self) -> None:
        common = {
            "production_id": self.production_id,
            "job_class": "STATE_PROPOSAL",
            "job_type": "SCRIPT_GENERATE",
            "semantic_capability": "text.generate",
            "spec": {"execution_mode": "PRIVATE", "projection": {}},
            "route": {"provider": "local", "execution_mode": "PRIVATE"},
            "input_fingerprint": "retry-policy-fingerprint",
            "cost_plan": CostPlan("text.generate", 0, 0),
        }
        two_attempts = self.kernel.jobs.admit(**common, max_attempts=2)
        one_attempt = self.kernel.jobs.admit(**common, max_attempts=1)
        two_again = self.kernel.jobs.admit(**common, max_attempts=2)

        self.assertFalse(two_attempts.reused)
        self.assertFalse(one_attempt.reused)
        self.assertNotEqual(one_attempt.job_id, two_attempts.job_id)
        self.assertTrue(two_again.reused)
        self.assertEqual(two_again.job_id, two_attempts.job_id)
        self.assertEqual(self.kernel.jobs.job(one_attempt.job_id)["max_attempts"], 1)
        self.assertEqual(self.kernel.jobs.job(two_attempts.job_id)["max_attempts"], 2)

    def test_max_attempts_requires_strict_positive_integer(self) -> None:
        with self.assertRaises(InvalidCommand):
            self.kernel.jobs.admit(
                production_id=self.production_id,
                job_class="STATE_PROPOSAL",
                job_type="SCRIPT_GENERATE",
                semantic_capability="text.generate",
                spec={"projection": {}},
                route={"provider": "local"},
                input_fingerprint="boolean-retry-limit",
                max_attempts=True,
            )

    def test_active_reuse_requires_same_spec_and_qualified_route(self) -> None:
        private = self.kernel.capabilities.request_state_job(
            production_id=self.production_id,
            capability="text.generate",
            execution_mode="PRIVATE",
        )
        private_again = self.kernel.capabilities.request_state_job(
            production_id=self.production_id,
            capability="text.generate",
            execution_mode="PRIVATE",
        )
        balanced = self.kernel.capabilities.request_state_job(
            production_id=self.production_id,
            capability="text.generate",
            execution_mode="BALANCED",
        )

        self.assertTrue(private_again.reused)
        self.assertEqual(private_again.job_id, private.job_id)
        self.assertFalse(balanced.reused)
        self.assertNotEqual(balanced.job_id, private.job_id)
        self.assertEqual(self.kernel.jobs.job(private.job_id)["spec"]["execution_mode"], "PRIVATE")
        self.assertEqual(self.kernel.jobs.job(balanced.job_id)["spec"]["execution_mode"], "BALANCED")
