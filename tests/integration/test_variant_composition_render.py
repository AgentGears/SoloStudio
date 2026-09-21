from __future__ import annotations

from tests.integration.job_test_support import JobTestCase


class VariantCompositionRenderTests(JobTestCase):
    def _capture_r1(self):
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=0,
            idempotency_key="script-r1",
            action="set_script",
            command_input={"text": "alpha script"},
        )
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=1,
            idempotency_key="visual-r1",
            action="set_visual_plan",
            command_input={
                "items": [
                    {"item_id": "scene-1", "description": "stable visual"},
                ]
            },
        )
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=2,
            idempotency_key="duration-r1",
            action="update_brief",
            command_input={
                "brief": {
                    "duration_min_ms": 1000,
                    "duration_max_ms": 1500,
                }
            },
        )
        return self.kernel.user.capture_revision(
            production_id=self.production_id,
            expected_state_version=3,
            idempotency_key="capture-r1",
        )

    @staticmethod
    def _intent():
        return {
            "aspect_ratio": "9:16",
            "language": "en",
            "duration_min_ms": 1000,
            "duration_max_ms": 1500,
            "caption_mode": "burned",
            "audio_mode": "voiceover",
        }

    def _materialize_inputs(self, variant_id: str):
        plans = self.kernel.variant_pipeline.plan_inputs(variant_id)
        for plan in plans:
            if plan.job_id is not None and plan.disposition in {"ADMITTED_JOB", "REUSED_JOB"}:
                job = self.kernel.jobs.job(plan.job_id)
                if job["state"] == "QUEUED":
                    self.kernel.derivations.execute_job(plan.job_id)
        return self.kernel.variant_pipeline.plan_inputs(variant_id)

    def _materialize_variant(self, variant_id: str):
        inputs = self._materialize_inputs(variant_id)
        composition_plan = self.kernel.variant_pipeline.plan_composition(variant_id)
        if composition_plan.job_id is not None:
            composition_id = self.kernel.variant_pipeline.execute_job(composition_plan.job_id)
        else:
            composition_id = str(composition_plan.artifact_id)
        render_plan = self.kernel.variant_pipeline.plan_render(variant_id)
        if render_plan.job_id is not None:
            render_id = self.kernel.variant_pipeline.execute_job(render_plan.job_id)
        else:
            render_id = str(render_plan.artifact_id)
        return inputs, composition_id, render_id

    def test_variant_lineage_composition_render_and_selective_recompute(self) -> None:
        r1 = self._capture_r1()
        v1 = self.kernel.variants.create(
            production_id=self.production_id,
            source_revision_id=r1.revision_id,
            intent=self._intent(),
        )
        inputs1, composition1_id, render1_id = self._materialize_variant(v1)
        by_role1 = {plan.output_role: plan for plan in inputs1}
        visual1_id = str(by_role1["visual.scene-1"].artifact_id)

        cover1 = self.kernel.variant_pipeline.plan_cover(visual1_id)
        self.assertEqual(cover1.disposition, "ADMITTED_JOB")
        cover1_id = self.kernel.variant_pipeline.execute_job(str(cover1.job_id))

        variant1 = self.kernel.variants.variant(v1)
        self.assertEqual(variant1["source_revision_id"], r1.revision_id)
        self.assertEqual(variant1["state"], "READY")
        composition1 = self.kernel.artifacts.artifact(composition1_id, verify_bytes=True)
        render1 = self.kernel.artifacts.artifact(render1_id, verify_bytes=True)
        self.assertEqual(composition1["variant_id"], v1)
        self.assertEqual(render1["variant_id"], v1)
        self.assertEqual(render1["metadata"]["validator_result"]["width"], 1080)
        self.assertEqual(render1["metadata"]["validator_result"]["height"], 1920)
        self.assertGreaterEqual(render1["metadata"]["validator_result"]["duration_ms"], 1000)
        self.assertEqual(
            {item["role"] for item in self.kernel.artifacts.dependencies(composition1_id)},
            {"voice", "captions", "visual.0000"},
        )
        self.assertEqual(
            self.kernel.artifacts.dependencies(render1_id),
            [{"source_artifact_id": composition1_id, "role": "composition_spec"}],
        )

        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=3,
            idempotency_key="script-r2",
            action="set_script",
            command_input={"text": "beta script"},
        )
        r2 = self.kernel.user.capture_revision(
            production_id=self.production_id,
            expected_state_version=4,
            idempotency_key="capture-r2",
        )
        v2 = self.kernel.variants.create(
            production_id=self.production_id,
            source_revision_id=r2.revision_id,
            parent_variant_id=v1,
            intent=self._intent(),
        )
        first_plan2 = self.kernel.variant_pipeline.plan_inputs(v2)
        first_by_role2 = {plan.output_role: plan for plan in first_plan2}
        self.assertEqual(first_by_role2["voice.primary"].disposition, "ADMITTED_JOB")
        self.assertEqual(first_by_role2["captions.primary"].disposition, "ADMITTED_JOB")
        self.assertEqual(first_by_role2["visual.scene-1"].disposition, "REUSED_ARTIFACT")
        self.assertEqual(first_by_role2["visual.scene-1"].artifact_id, visual1_id)
        for plan in first_plan2:
            if plan.job_id is not None:
                self.kernel.derivations.execute_job(plan.job_id)

        inputs2, composition2_id, render2_id = self._materialize_variant(v2)
        by_role2 = {plan.output_role: plan for plan in inputs2}
        self.assertEqual(by_role2["visual.scene-1"].artifact_id, visual1_id)
        self.assertNotEqual(composition2_id, composition1_id)
        self.assertNotEqual(render2_id, render1_id)

        cover2 = self.kernel.variant_pipeline.plan_cover(str(by_role2["visual.scene-1"].artifact_id))
        self.assertEqual(cover2.disposition, "REUSED_ARTIFACT")
        self.assertEqual(cover2.artifact_id, cover1_id)

        variant1_after = self.kernel.variants.variant(v1)
        variant2 = self.kernel.variants.variant(v2)
        self.assertEqual(variant1_after["source_revision_id"], r1.revision_id)
        self.assertEqual(variant2["source_revision_id"], r2.revision_id)
        self.assertEqual(variant2["parent_variant_id"], v1)
        self.assertEqual(variant1_after["state"], "READY")
        self.assertEqual(variant2["state"], "READY")
        self.assertEqual(self.kernel.artifacts.artifact(render1_id)["variant_id"], v1)
        self.assertEqual(self.kernel.artifacts.artifact(render2_id)["variant_id"], v2)

    def test_equivalent_variant_creation_is_idempotent_but_lineage_metadata_cannot_drift(self) -> None:
        r1 = self._capture_r1()
        first = self.kernel.variants.create(
            production_id=self.production_id,
            source_revision_id=r1.revision_id,
            intent=self._intent(),
        )
        replay = self.kernel.variants.create(
            production_id=self.production_id,
            source_revision_id=r1.revision_id,
            intent=self._intent(),
        )
        self.assertEqual(first, replay)

    def test_render_reuse_is_variant_scoped_even_when_material_context_matches(self) -> None:
        r1 = self._capture_r1()
        v1 = self.kernel.variants.create(
            production_id=self.production_id,
            source_revision_id=r1.revision_id,
            intent=self._intent(),
        )
        _inputs, _composition, render1 = self._materialize_variant(v1)

        square_intent = dict(self._intent())
        square_intent["aspect_ratio"] = "1:1"
        v2 = self.kernel.variants.create(
            production_id=self.production_id,
            source_revision_id=r1.revision_id,
            parent_variant_id=v1,
            intent=square_intent,
        )
        self._materialize_inputs(v2)
        composition2 = self.kernel.variant_pipeline.plan_composition(v2)
        self.assertEqual(composition2.disposition, "ADMITTED_JOB")
        self.kernel.variant_pipeline.execute_job(str(composition2.job_id))
        render2 = self.kernel.variant_pipeline.plan_render(v2)
        self.assertEqual(render2.disposition, "ADMITTED_JOB")
        render2_id = self.kernel.variant_pipeline.execute_job(str(render2.job_id))
        self.assertNotEqual(render1, render2_id)
        self.assertEqual(self.kernel.artifacts.artifact(render2_id)["variant_id"], v2)
