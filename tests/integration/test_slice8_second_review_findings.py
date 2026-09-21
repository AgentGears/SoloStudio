from __future__ import annotations

import json
import subprocess

from solostudio.kernel.errors import InvalidArtifact
from tests.integration.job_test_support import JobTestCase


class Slice8SecondReviewFindingTests(JobTestCase):
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

    def _materialize_variant(self, variant_id: str) -> tuple[str, str, str]:
        plans = self.kernel.variant_pipeline.plan_inputs(variant_id)
        voice_plan = next(plan for plan in plans if plan.output_role == "voice.primary")
        if voice_plan.job_id is not None and self.kernel.jobs.job(voice_plan.job_id)["state"] == "QUEUED":
            self.kernel.derivations.execute_job(voice_plan.job_id)
        voice_plan = next(
            plan
            for plan in self.kernel.variant_pipeline.plan_inputs(variant_id)
            if plan.output_role == "voice.primary"
        )
        self.assertEqual(voice_plan.disposition, "REUSED_ARTIFACT")

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
        return str(voice_plan.artifact_id), composition_id, render_id

    def test_non_mp4_container_cannot_be_registered_as_video_mp4(self) -> None:
        path = self.root / "pretend.mp4"
        completed = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x240:r=30",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000",
                "-t",
                "1.000",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-f",
                "matroska",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertGreater(path.stat().st_size, 1024)

        with self.assertRaisesRegex(InvalidArtifact, "not MP4"):
            self.kernel.artifacts.prepare_bytes(
                path.read_bytes(),
                kind="rendered_video",
                media_type="video/mp4",
                producer_stage="container-authority-test",
            )

    def test_composition_preferences_change_composition_and_render_bytes(self) -> None:
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=0,
            idempotency_key="prefs-script-r1",
            action="set_script",
            command_input={"text": "stable script"},
        )
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=1,
            idempotency_key="prefs-duration-r1",
            action="update_brief",
            command_input={
                "brief": {
                    "duration_min_ms": 1000,
                    "duration_max_ms": 1500,
                }
            },
        )
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=2,
            idempotency_key="prefs-r1",
            action="set_composition_preferences",
            command_input={"preferences": {"layout": "tight", "safe_margin_bp": 500}},
        )
        r1 = self.kernel.user.capture_revision(
            production_id=self.production_id,
            expected_state_version=3,
            idempotency_key="prefs-capture-r1",
        )
        v1 = self.kernel.variants.create(
            production_id=self.production_id,
            source_revision_id=r1.revision_id,
            intent=self._intent(),
        )
        voice1_id, composition1_id, render1_id = self._materialize_variant(v1)
        composition1 = self.kernel.artifacts.artifact(composition1_id, verify_bytes=True)
        render1 = self.kernel.artifacts.artifact(render1_id, verify_bytes=True)
        composition1_payload = json.loads(self.kernel.artifacts.read_bytes(composition1_id).decode("utf-8"))
        self.assertEqual(
            composition1_payload["composition_preferences"],
            {"layout": "tight", "safe_margin_bp": 500},
        )

        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=3,
            idempotency_key="prefs-r2",
            action="set_composition_preferences",
            command_input={"preferences": {"layout": "spacious", "safe_margin_bp": 800}},
        )
        r2 = self.kernel.user.capture_revision(
            production_id=self.production_id,
            expected_state_version=4,
            idempotency_key="prefs-capture-r2",
        )
        v2 = self.kernel.variants.create(
            production_id=self.production_id,
            source_revision_id=r2.revision_id,
            parent_variant_id=v1,
            intent=self._intent(),
        )

        input_plans2 = self.kernel.variant_pipeline.plan_inputs(v2)
        voice2 = next(plan for plan in input_plans2 if plan.output_role == "voice.primary")
        self.assertEqual(voice2.disposition, "REUSED_ARTIFACT")
        self.assertEqual(voice2.artifact_id, voice1_id)

        composition2_plan = self.kernel.variant_pipeline.plan_composition(v2)
        self.assertEqual(composition2_plan.disposition, "ADMITTED_JOB")
        composition2_id = self.kernel.variant_pipeline.execute_job(str(composition2_plan.job_id))
        composition2 = self.kernel.artifacts.artifact(composition2_id, verify_bytes=True)
        composition2_payload = json.loads(self.kernel.artifacts.read_bytes(composition2_id).decode("utf-8"))
        self.assertEqual(
            composition2_payload["composition_preferences"],
            {"layout": "spacious", "safe_margin_bp": 800},
        )
        self.assertNotEqual(composition1["input_fingerprint"], composition2["input_fingerprint"])
        self.assertNotEqual(composition1["object_digest"], composition2["object_digest"])

        render2_plan = self.kernel.variant_pipeline.plan_render(v2)
        self.assertEqual(render2_plan.disposition, "ADMITTED_JOB")
        render2_id = self.kernel.variant_pipeline.execute_job(str(render2_plan.job_id))
        render2 = self.kernel.artifacts.artifact(render2_id, verify_bytes=True)
        self.assertNotEqual(render1["input_fingerprint"], render2["input_fingerprint"])
        self.assertNotEqual(render1["object_digest"], render2["object_digest"])
        self.assertEqual(self.kernel.variants.variant(v1)["state"], "READY")
        self.assertEqual(self.kernel.variants.variant(v2)["state"], "READY")
