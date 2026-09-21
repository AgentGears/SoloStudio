from __future__ import annotations

from solostudio.kernel.errors import InvalidArtifact
from solostudio.kernel.identity import canonical_text
from tests.integration.job_test_support import JobTestCase


class Slice8IsolatedSecondPassTests(JobTestCase):
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

    def _variant_with_materialized_inputs(self) -> tuple[str, str]:
        revision = self.capture_revision()
        variant_id = self.kernel.variants.create(
            production_id=self.production_id,
            source_revision_id=revision.revision_id,
            intent=self._intent(),
        )
        for plan in self.kernel.variant_pipeline.plan_inputs(variant_id):
            if plan.job_id is not None and self.kernel.jobs.job(plan.job_id)["state"] == "QUEUED":
                self.kernel.derivations.execute_job(plan.job_id)
        return revision.revision_id, variant_id

    def test_composition_registration_rejects_extra_noncompiler_field(self) -> None:
        _revision_id, variant_id = self._variant_with_materialized_inputs()
        plan = self.kernel.variant_pipeline.plan_composition(variant_id)
        self.assertIsNotNone(plan.job_id)
        self.assertIsNotNone(plan.attempt_id)

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
        composition["unclaimed_extension"] = {"changes_bytes": True}

        attempt_id = str(plan.attempt_id)
        temp_dir = self.kernel.jobs.start_attempt(
            attempt_id,
            "deterministic-artifact-provider",
        )
        (temp_dir / "composition.json").write_bytes(
            canonical_text(composition).encode("utf-8")
        )
        with self.assertRaisesRegex(
            InvalidArtifact,
            "deterministic compiler output",
        ):
            self.kernel.jobs.complete_artifact_attempt(
                attempt_id,
                [
                    {
                        "role": "composition.primary",
                        "path": "composition.json",
                        "kind": "composition_spec",
                        "media_type": "application/json",
                        "producer_stage": "variant_composition_compiler",
                    }
                ],
            )
        self.kernel.jobs.fail_attempt(
            attempt_id,
            "EXPECTED_TEST_FAILURE",
            "composition deterministic-byte authority proof",
        )
        with self.kernel.store.read() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM artifacts WHERE variant_id=? AND kind='composition_spec'",
                (variant_id,),
            ).fetchone()[0]
        self.assertEqual(int(count), 0)

    def test_render_registration_rejects_alternate_valid_media_bytes(self) -> None:
        _revision_id, variant_id = self._variant_with_materialized_inputs()
        composition_plan = self.kernel.variant_pipeline.plan_composition(variant_id)
        self.assertIsNotNone(composition_plan.job_id)
        composition_id = self.kernel.variant_pipeline.execute_job(str(composition_plan.job_id))

        render_plan = self.kernel.variant_pipeline.plan_render(variant_id)
        self.assertIsNotNone(render_plan.job_id)
        self.assertIsNotNone(render_plan.attempt_id)
        render_job = self.kernel.jobs.job(str(render_plan.job_id))
        composition_bytes = self.kernel.artifacts.read_bytes(composition_id)
        import json

        composition = json.loads(composition_bytes.decode("utf-8"))
        self.assertIsInstance(composition, dict)

        attempt_id = str(render_plan.attempt_id)
        temp_dir = self.kernel.jobs.start_attempt(
            attempt_id,
            "deterministic-media-renderer",
        )
        alternate_fingerprint = "0" * 64
        if alternate_fingerprint == str(render_job["input_fingerprint"]):
            alternate_fingerprint = "f" * 64
        self.kernel.variant_pipeline._render_fixture(
            temp_dir / "render.mp4",
            composition,
            alternate_fingerprint,
        )

        with self.assertRaisesRegex(
            InvalidArtifact,
            "deterministic media fixture output",
        ):
            self.kernel.jobs.complete_artifact_attempt(
                attempt_id,
                [
                    {
                        "role": "render.primary",
                        "path": "render.mp4",
                        "kind": "rendered_video",
                        "media_type": "video/mp4",
                        "producer_stage": "local_media_renderer",
                    }
                ],
            )
        self.kernel.jobs.fail_attempt(
            attempt_id,
            "EXPECTED_TEST_FAILURE",
            "render deterministic-byte authority proof",
        )
        self.assertEqual(self.kernel.variants.variant(variant_id)["state"], "PROPOSED")
        with self.kernel.store.read() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM artifacts WHERE variant_id=? AND kind='rendered_video'",
                (variant_id,),
            ).fetchone()[0]
        self.assertEqual(int(count), 0)
