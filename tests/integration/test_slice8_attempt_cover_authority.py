from __future__ import annotations

from solostudio.kernel.errors import InvalidArtifact, InvalidCommand
from solostudio.kernel.identity import canonical_text
from tests.integration.job_test_support import JobTestCase


class Slice8AttemptAndCoverAuthorityTests(JobTestCase):
    @staticmethod
    def _intent() -> dict[str, object]:
        return {
            "aspect_ratio": "9:16",
            "language": "en",
            "duration_min_ms": 1000,
            "duration_max_ms": 1500,
            "caption_mode": "none",
            "audio_mode": "voiceover",
        }

    def _materialize_variant_inputs(self, variant_id: str) -> None:
        for plan in self.kernel.variant_pipeline.plan_inputs(variant_id):
            if plan.job_id is not None and self.kernel.jobs.job(plan.job_id)["state"] == "QUEUED":
                self.kernel.derivations.execute_job(plan.job_id)

    def _revision_with_visual(self):
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=0,
            idempotency_key="attempt-cover-script",
            action="set_script",
            command_input={"text": "cover authority"},
        )
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=1,
            idempotency_key="attempt-cover-visual",
            action="set_visual_plan",
            command_input={
                "items": [
                    {"item_id": "scene-01", "purpose": "cover", "prompt": "authority proof"},
                ]
            },
        )
        return self.kernel.user.capture_revision(
            production_id=self.production_id,
            expected_state_version=2,
            idempotency_key="attempt-cover-capture",
        )

    def test_cover_planning_rejects_imported_visual_before_job_or_cost_admission(self) -> None:
        revision = self._revision_with_visual()
        for plan in self.kernel.derivations.plan_revision(revision.revision_id):
            if plan.job_id is not None and self.kernel.jobs.job(plan.job_id)["state"] == "QUEUED":
                self.kernel.derivations.execute_job(plan.job_id)
        materialized = {
            plan.output_role: plan
            for plan in self.kernel.derivations.plan_revision(revision.revision_id)
        }
        legitimate_id = str(materialized["visual.scene-01"].artifact_id)
        legitimate = self.kernel.artifacts.artifact(legitimate_id, verify_bytes=True)
        forged_id = self.kernel.artifacts.promote_and_register(
            self.kernel.artifacts.read_bytes(legitimate_id),
            production_id=self.production_id,
            production_revision_id=revision.revision_id,
            kind="visual_image",
            media_type="image/png",
            producer_stage="imported-visual-without-producer",
            input_fingerprint=str(legitimate["input_fingerprint"]),
        )
        forged = self.kernel.artifacts.artifact(forged_id, verify_bytes=True)
        self.assertIsNone(forged["producer_job_id"])
        self.assertIsNone(forged["producer_attempt_id"])

        with self.kernel.store.read() as db:
            jobs_before = int(db.execute("SELECT COUNT(*) FROM job_specs").fetchone()[0])
            costs_before = int(db.execute("SELECT COUNT(*) FROM cost_ledger").fetchone()[0])
        with self.assertRaisesRegex(InvalidCommand, "authoritative revision-derivation producer"):
            self.kernel.variant_pipeline.plan_cover(forged_id)
        with self.kernel.store.read() as db:
            self.assertEqual(int(db.execute("SELECT COUNT(*) FROM job_specs").fetchone()[0]), jobs_before)
            self.assertEqual(int(db.execute("SELECT COUNT(*) FROM cost_ledger").fetchone()[0]), costs_before)

    def test_composition_registration_requires_running_bound_producer_attempt(self) -> None:
        revision = self.capture_revision()
        variant_id = self.kernel.variants.create(
            production_id=self.production_id,
            source_revision_id=revision.revision_id,
            intent=self._intent(),
        )
        self._materialize_variant_inputs(variant_id)
        composition_plan = self.kernel.variant_pipeline.plan_composition(variant_id)
        self.assertIsNotNone(composition_plan.job_id)
        self.assertIsNotNone(composition_plan.attempt_id)
        job = self.kernel.jobs.job(str(composition_plan.job_id))
        self.assertEqual(job["state"], "QUEUED")
        self.assertEqual(self.kernel.jobs.attempt(str(composition_plan.attempt_id))["state"], "CREATED")

        context = self.kernel.variant_pipeline._materialized_composition_context(
            variant_id,
            execution_mode="PRIVATE",
            max_cost_microunits=0,
            max_attempts=2,
        )
        sources = {
            role: self.kernel.artifacts.artifact(artifact_id, verify_bytes=True)
            for artifact_id, role in context.source_artifacts
        }
        composition = self.kernel.variant_pipeline._compile_composition(
            self.kernel.variants.variant(variant_id),
            sources,
            context.composition_preferences,
        )
        prepared = self.kernel.artifacts.prepare_bytes(
            canonical_text(composition).encode("utf-8"),
            kind="composition_spec",
            media_type="application/json",
            producer_stage="queued-job-registration-bypass",
            input_fingerprint=str(job["input_fingerprint"]),
        )

        with self.assertRaisesRegex(InvalidArtifact, "running producer Attempt"):
            with self.kernel.store.write() as db:
                self.kernel.artifacts.register_prepared_in_tx(
                    db,
                    prepared,
                    production_id=self.production_id,
                    production_revision_id=revision.revision_id,
                    variant_id=variant_id,
                    producer_job_id=str(composition_plan.job_id),
                    producer_attempt_id=None,
                )

        self.assertEqual(self.kernel.jobs.job(str(composition_plan.job_id))["state"], "QUEUED")
        self.assertEqual(self.kernel.jobs.attempt(str(composition_plan.attempt_id))["state"], "CREATED")
        with self.kernel.store.read() as db:
            registered = db.execute(
                "SELECT COUNT(*) FROM artifacts WHERE producer_job_id=? AND kind='composition_spec'",
                (str(composition_plan.job_id),),
            ).fetchone()[0]
        self.assertEqual(int(registered), 0)
