from __future__ import annotations

from solostudio.app.bootstrap import bootstrap
from solostudio.kernel.clock import FixedClock
from solostudio.kernel.costs import CostPlan
from solostudio.kernel.errors import BudgetExceeded, InvalidCommand
from solostudio.kernel.identity import canonical_hash
from tests.integration.job_test_support import JobTestCase


class CapabilityRoutingCostTests(JobTestCase):
    def _set_brief(self) -> None:
        result = self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=0,
            idempotency_key="brief",
            action="update_brief",
            command_input={
                "brief": {
                    "topic": "Why compact models matter",
                    "audience": "technical creators",
                    "goal": "explain the practical trade-off",
                    "tone": "technical",
                    "primary_language": "en",
                }
            },
        )
        self.assertEqual(result.classification, "COMMITTED")

    def test_route_is_persisted_before_identity_and_material_to_fingerprint(self) -> None:
        self._set_brief()
        admission = self.kernel.capabilities.request_state_job(
            production_id=self.production_id,
            capability="text.generate",
        )
        job = self.kernel.jobs.job(admission.job_id)
        route = job["route"]
        identity = {
            "route_id": route["route_id"],
            "provider": route["provider"],
            "model": route["model"],
            "tool_profile": route["tool_profile"],
        }
        expected = canonical_hash({
            "capability": "text.generate",
            "source_state_version": 1,
            "projection": job["spec"]["projection"],
            "route_identity": identity,
        })
        changed = dict(identity)
        changed["model"] = "different-model"
        changed_hash = canonical_hash({
            "capability": "text.generate",
            "source_state_version": 1,
            "projection": job["spec"]["projection"],
            "route_identity": changed,
        })
        self.assertEqual(job["input_fingerprint"], expected)
        self.assertNotEqual(job["input_fingerprint"], changed_hash)
        self.assertEqual(route["qualification_evidence"]["locality"], "LOCAL")


    def test_state_change_between_projection_and_admission_is_rejected(self) -> None:
        original = self.kernel.capabilities.router.qualify

        def qualify_then_edit(capability: str, *, execution_mode: str):
            route = original(capability, execution_mode=execution_mode)
            self.kernel.user.command(
                production_id=self.production_id,
                expected_state_version=0,
                idempotency_key="race-edit",
                action="set_script",
                command_input={"text": "newer producer state"},
            )
            return route

        self.kernel.capabilities.router.qualify = qualify_then_edit
        with self.assertRaises(InvalidCommand):
            self.kernel.capabilities.request_state_job(
                production_id=self.production_id,
                capability="text.generate",
            )
        with self.kernel.store.read() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM job_specs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM cost_ledger").fetchone()[0], 0)

    def test_budget_check_occurs_after_route_estimate_before_admission(self) -> None:
        original = self.kernel.capabilities.router.qualify

        def qualified_nonzero(capability: str, *, execution_mode: str):
            route = original(capability, execution_mode=execution_mode)
            route["estimated_cost_microunits"] = 5
            return route

        self.kernel.capabilities.router.qualify = qualified_nonzero
        with self.assertRaises(BudgetExceeded):
            self.kernel.capabilities.request_state_job(
                production_id=self.production_id,
                capability="text.generate",
                max_cost_microunits=0,
            )
        with self.kernel.store.read() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM job_specs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM cost_ledger").fetchone()[0], 0)

    def test_active_equivalent_reuses_job_and_single_reservation(self) -> None:
        first = self.kernel.capabilities.request_state_job(
            production_id=self.production_id,
            capability="text.generate",
        )
        second = self.kernel.capabilities.request_state_job(
            production_id=self.production_id,
            capability="text.generate",
        )
        self.assertFalse(first.reused)
        self.assertTrue(second.reused)
        self.assertEqual(second.job_id, first.job_id)
        self.assertEqual(second.cost_id, first.cost_id)
        with self.kernel.store.read() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM cost_ledger WHERE job_id=?", (first.job_id,)).fetchone()[0], 1)
        self.assertEqual(self.kernel.costs.for_job(first.job_id)["state"], "RESERVED")

    def test_deterministic_state_jobs_settle_then_adopt_and_capture_r1_inputs(self) -> None:
        self._set_brief()
        script = self.kernel.capabilities.request_state_job(
            production_id=self.production_id,
            capability="text.generate",
        )
        self.assertEqual(self.kernel.costs.for_job(script.job_id)["state"], "RESERVED")
        script_run = self.kernel.capabilities.execute_state_job(script.job_id)
        self.assertEqual(script_run["proposal"]["action"], "set_script")
        self.assertEqual(self.kernel.costs.for_job(script.job_id)["state"], "SETTLED")
        with self.kernel.store.read() as db:
            events = [
                row[0]
                for row in db.execute(
                    "SELECT event_type FROM journal_entries WHERE entity_type='cost_ledger' AND entity_id=? ORDER BY seq",
                    (script.cost_id,),
                )
            ]
        self.assertEqual(events, ["COST_ESTIMATED", "COST_RESERVED", "COST_SETTLED"])
        adopted_script = self.kernel.jobs.adopt_state_proposal(script.job_id, self.kernel.user.principal, "adopt-script")
        self.assertEqual(adopted_script.classification, "COMMITTED")

        visuals = self.kernel.capabilities.request_state_job(
            production_id=self.production_id,
            capability="visual.plan",
        )
        self.assertEqual(self.kernel.costs.for_job(visuals.job_id)["state"], "RESERVED")
        visual_run = self.kernel.capabilities.execute_state_job(visuals.job_id)
        self.assertEqual(visual_run["proposal"]["action"], "set_visual_plan")
        self.assertEqual(self.kernel.costs.for_job(visuals.job_id)["state"], "SETTLED")
        adopted_visuals = self.kernel.jobs.adopt_state_proposal(visuals.job_id, self.kernel.user.principal, "adopt-visuals")
        self.assertEqual(adopted_visuals.classification, "COMMITTED")

        revision = self.kernel.user.capture_revision(
            production_id=self.production_id,
            expected_state_version=3,
            idempotency_key="capture-r1",
        )
        artifacts = self.kernel.artifacts.revision_artifacts(revision.revision_id, verify_bytes=True)
        self.assertEqual({artifact["kind"] for artifact in artifacts}, {"script_text", "visual_plan"})
        state = self.kernel.productions.working_state(self.production_id)
        self.assertEqual(state["state"]["script"]["status"], "ready")
        self.assertEqual(len(state["state"]["visual_plan"]), 3)


    def test_executor_rejects_mismatched_route_before_attempt_start(self) -> None:
        route = self.kernel.capabilities.router.qualify("text.generate", execution_mode="PRIVATE")
        bad_route = dict(route)
        bad_route["model"] = "unqualified-model"
        admission = self.kernel.jobs.admit(
            production_id=self.production_id,
            job_class="STATE_PROPOSAL",
            job_type="SCRIPT_GENERATE",
            semantic_capability="text.generate",
            spec={
                "schema_version": 1,
                "execution_mode": "PRIVATE",
                "projection": {"brief": {
                    "topic": "x", "audience": "y", "goal": "z", "tone": "clear",
                    "duration_min_ms": 45000, "duration_max_ms": 60000, "primary_language": "en",
                }},
            },
            route=bad_route,
            input_fingerprint="bad-route",
        )
        with self.assertRaises(InvalidCommand):
            self.kernel.capabilities.execute_state_job(admission.job_id)
        self.assertEqual(self.kernel.jobs.job(admission.job_id)["state"], "QUEUED")
        self.assertEqual(self.kernel.jobs.attempt(admission.attempt_id)["state"], "CREATED")

    def test_invalid_projection_is_rejected_before_attempt_start(self) -> None:
        route = self.kernel.capabilities.router.qualify("text.generate", execution_mode="PRIVATE")
        admission = self.kernel.jobs.admit(
            production_id=self.production_id,
            job_class="STATE_PROPOSAL",
            job_type="SCRIPT_GENERATE",
            semantic_capability="text.generate",
            spec={"schema_version": 1, "execution_mode": "PRIVATE", "projection": {}},
            route=route,
            input_fingerprint="bad-projection",
        )
        with self.assertRaises(InvalidCommand):
            self.kernel.capabilities.execute_state_job(admission.job_id)
        self.assertEqual(self.kernel.jobs.job(admission.job_id)["state"], "QUEUED")
        self.assertEqual(self.kernel.jobs.attempt(admission.attempt_id)["state"], "CREATED")

    def test_cost_plan_capability_mismatch_is_rejected_without_ledger_row(self) -> None:
        with self.assertRaises(InvalidCommand):
            self.kernel.jobs.admit(
                production_id=self.production_id,
                job_class="STATE_PROPOSAL",
                job_type="SCRIPT_GENERATE",
                semantic_capability="text.generate",
                spec={"projection": {}},
                route={"billing_ambiguity_on_interrupt": False},
                input_fingerprint="cost-mismatch",
                cost_plan=CostPlan("visual.plan", 0, 0),
            )
        with self.kernel.store.read() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM cost_ledger").fetchone()[0], 0)

    def test_cost_values_require_strict_integer_microunits(self) -> None:
        with self.assertRaises(ValueError):
            CostPlan("text.generate", True, 0)
        with self.assertRaises(InvalidCommand):
            self.kernel.capabilities.request_state_job(
                production_id=self.production_id,
                capability="text.generate",
                max_cost_microunits=True,
            )

    def test_malformed_route_cost_estimate_is_rejected_before_admission(self) -> None:
        original = self.kernel.capabilities.router.qualify

        def invalid_estimate(capability: str, *, execution_mode: str):
            route = original(capability, execution_mode=execution_mode)
            route["estimated_cost_microunits"] = "0"
            return route

        self.kernel.capabilities.router.qualify = invalid_estimate
        with self.assertRaises(InvalidCommand):
            self.kernel.capabilities.request_state_job(
                production_id=self.production_id,
                capability="text.generate",
            )
        with self.kernel.store.read() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM job_specs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM cost_ledger").fetchone()[0], 0)

    def test_clean_failure_releases_reservation(self) -> None:
        admission = self.kernel.jobs.admit(
            production_id=self.production_id,
            job_class="STATE_PROPOSAL",
            job_type="SCRIPT_GENERATE",
            semantic_capability="text.generate",
            spec={"projection": {}},
            route={"billing_ambiguity_on_interrupt": False},
            input_fingerprint="clean-failure",
            cost_plan=CostPlan("text.generate", 0, 0),
        )
        self.kernel.jobs.start_attempt(admission.attempt_id, "test-provider")
        self.kernel.jobs.fail_attempt(admission.attempt_id, "TEST_FAILURE", "clean failure")
        self.assertEqual(self.kernel.jobs.job(admission.job_id)["state"], "FAILED")
        self.assertEqual(self.kernel.costs.for_job(admission.job_id)["state"], "RELEASED")

    def test_unambiguous_restart_requeues_and_keeps_reservation(self) -> None:
        admission = self.kernel.jobs.admit(
            production_id=self.production_id,
            job_class="STATE_PROPOSAL",
            job_type="SCRIPT_GENERATE",
            semantic_capability="text.generate",
            spec={"projection": {}},
            route={"billing_ambiguity_on_interrupt": False},
            input_fingerprint="retry-cost",
            max_attempts=2,
            cost_plan=CostPlan("text.generate", 0, 0),
        )
        self.kernel.jobs.start_attempt(admission.attempt_id, "test-provider")
        self.kernel.close()
        self.kernel = bootstrap(self.root, clock=FixedClock(), ids=self.ids)
        self.assertEqual([item["state"] for item in self.kernel.jobs.attempts(admission.job_id)], ["INTERRUPTED", "CREATED"])
        self.assertEqual(self.kernel.jobs.job(admission.job_id)["state"], "QUEUED")
        self.assertEqual(self.kernel.costs.for_job(admission.job_id)["state"], "RESERVED")

    def test_ambiguous_restart_marks_cost_unknown_and_does_not_retry(self) -> None:
        admission = self.kernel.jobs.admit(
            production_id=self.production_id,
            job_class="STATE_PROPOSAL",
            job_type="SCRIPT_GENERATE",
            semantic_capability="text.generate",
            spec={"projection": {}},
            route={"billing_ambiguity_on_interrupt": True},
            input_fingerprint="ambiguous-cost",
            max_attempts=2,
            cost_plan=CostPlan("text.generate", 0, 0),
        )
        self.kernel.jobs.start_attempt(admission.attempt_id, "test-provider")
        self.kernel.close()
        self.kernel = bootstrap(self.root, clock=FixedClock(), ids=self.ids)
        self.assertEqual([item["state"] for item in self.kernel.jobs.attempts(admission.job_id)], ["INTERRUPTED"])
        self.assertEqual(self.kernel.jobs.job(admission.job_id)["state"], "FAILED")
        self.assertEqual(self.kernel.costs.for_job(admission.job_id)["state"], "UNKNOWN")
