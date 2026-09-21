from __future__ import annotations

from solostudio.kernel.errors import InvalidCommand
from tests.integration.job_test_support import JobTestCase


class Slice8CoverPreferenceTests(JobTestCase):
    def _visual_artifact(self) -> str:
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=0,
            idempotency_key="cover-visual-plan",
            action="set_visual_plan",
            command_input={
                "items": [
                    {"item_id": "cover-source", "description": "stable cover source"},
                ]
            },
        )
        revision = self.kernel.user.capture_revision(
            production_id=self.production_id,
            expected_state_version=1,
            idempotency_key="cover-capture",
        )
        plan = next(
            item
            for item in self.kernel.derivations.plan_revision(revision.revision_id)
            if item.output_role == "visual.cover-source"
        )
        self.kernel.derivations.execute_job(str(plan.job_id))
        materialized = next(
            item
            for item in self.kernel.derivations.plan_revision(revision.revision_id)
            if item.output_role == "visual.cover-source"
        )
        self.assertEqual(materialized.disposition, "REUSED_ARTIFACT")
        return str(materialized.artifact_id)

    def test_nonempty_cover_preferences_fail_before_job_or_cost_admission(self) -> None:
        visual_id = self._visual_artifact()
        with self.kernel.store.read() as db:
            jobs_before = db.execute(
                "SELECT COUNT(*) FROM job_specs WHERE semantic_capability='cover.produce'"
            ).fetchone()[0]
            costs_before = db.execute(
                "SELECT COUNT(*) FROM cost_ledger WHERE capability='cover.produce'"
            ).fetchone()[0]

        with self.assertRaisesRegex(
            InvalidCommand,
            "non-empty cover preferences are unsupported",
        ):
            self.kernel.variant_pipeline.plan_cover(
                visual_id,
                cover_preferences={"layout": "unsupported"},
            )

        with self.kernel.store.read() as db:
            jobs_after = db.execute(
                "SELECT COUNT(*) FROM job_specs WHERE semantic_capability='cover.produce'"
            ).fetchone()[0]
            costs_after = db.execute(
                "SELECT COUNT(*) FROM cost_ledger WHERE capability='cover.produce'"
            ).fetchone()[0]
        self.assertEqual(jobs_after, jobs_before)
        self.assertEqual(costs_after, costs_before)

    def test_default_cover_contract_is_material_and_reusable(self) -> None:
        visual_id = self._visual_artifact()
        source = self.kernel.artifacts.artifact(visual_id, verify_bytes=True)

        planned = self.kernel.variant_pipeline.plan_cover(visual_id)
        self.assertEqual(planned.disposition, "ADMITTED_JOB")
        cover_id = self.kernel.variant_pipeline.execute_job(str(planned.job_id))
        cover = self.kernel.artifacts.artifact(cover_id, verify_bytes=True)
        self.assertEqual(cover["object_digest"], source["object_digest"])

        reused = self.kernel.variant_pipeline.plan_cover(
            visual_id,
            cover_preferences={},
        )
        self.assertEqual(reused.disposition, "REUSED_ARTIFACT")
        self.assertEqual(reused.artifact_id, cover_id)
