from __future__ import annotations

import json
import subprocess

from solostudio.kernel.derivations.fingerprints import expected_fingerprint
from solostudio.kernel.errors import InvalidArtifact
from solostudio.kernel.identity import canonical_text
from tests.integration.job_test_support import JobTestCase


class Slice8FinalReviewFindingTests(JobTestCase):
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
            if plan.job_id is None:
                continue
            if self.kernel.jobs.job(plan.job_id)["state"] == "QUEUED":
                self.kernel.derivations.execute_job(plan.job_id)

    def test_composition_registration_rejects_stale_same_production_source(self) -> None:
        r1 = self.capture_revision()
        voice_plan = next(
            plan
            for plan in self.kernel.derivations.plan_revision(r1.revision_id)
            if plan.output_role == "voice.primary"
        )
        if voice_plan.artifact_id is not None:
            old_voice_id = str(voice_plan.artifact_id)
        else:
            self.assertIsNotNone(voice_plan.job_id)
            old_voice_id = self.kernel.derivations.execute_job(str(voice_plan.job_id))
        old_voice = self.kernel.artifacts.artifact(old_voice_id, verify_bytes=True)

        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=1,
            idempotency_key="stale-source-r2-script",
            action="set_script",
            command_input={"text": "new script for current revision"},
        )
        r2 = self.kernel.user.capture_revision(
            production_id=self.production_id,
            expected_state_version=2,
            idempotency_key="stale-source-r2-capture",
        )
        variant_id = self.kernel.variants.create(
            production_id=self.production_id,
            source_revision_id=r2.revision_id,
            intent=self._intent(),
        )
        variant = self.kernel.variants.variant(variant_id)
        route = self.kernel.capabilities.router.qualify(
            "composition.compile",
            execution_mode="PRIVATE",
        )
        semantic_inputs = {
            "variant_intent_hash": str(variant["intent_hash"]),
            "composition_preferences": {},
        }
        source_digests = {"voice": str(old_voice["object_digest"])}
        fingerprint = expected_fingerprint(
            "composition.compile",
            output_role="composition.primary",
            semantic_inputs=semantic_inputs,
            source_object_digests=source_digests,
            route=route,
        )
        admission = self.kernel.jobs.admit(
            production_id=self.production_id,
            production_revision_id=r2.revision_id,
            variant_id=variant_id,
            job_class="ARTIFACT",
            job_type="COMPOSITION_COMPILE",
            semantic_capability="composition.compile",
            spec={
                "schema_version": 1,
                "execution_mode": "PRIVATE",
                "output_role": "composition.primary",
                "kind": "composition_spec",
                "media_type": "application/json",
                "filename": "composition.json",
                "semantic_inputs": semantic_inputs,
                "source_object_digests": source_digests,
                "source_artifacts": [
                    {"artifact_id": old_voice_id, "role": "voice"},
                ],
            },
            route=route,
            input_fingerprint=fingerprint,
            max_attempts=1,
        )
        malicious_composition = {
            "schema_version": 1,
            "variant_intent_hash": str(variant["intent_hash"]),
            "composition_preferences": {},
            "canvas": self.kernel.variants.canvas(self._intent()),
            "duration_ms": 1000,
            "tracks": [
                {"kind": "visual", "items": []},
                {"kind": "voice", "object_digest": str(old_voice["object_digest"])},
            ],
        }
        temp_dir = self.kernel.jobs.start_attempt(
            admission.attempt_id,
            "deterministic-artifact-provider",
        )
        (temp_dir / "composition.json").write_bytes(
            canonical_text(malicious_composition).encode("utf-8")
        )
        with self.assertRaisesRegex(InvalidArtifact, "not current"):
            self.kernel.jobs.complete_artifact_attempt(
                admission.attempt_id,
                [
                    {
                        "role": "composition.primary",
                        "path": "composition.json",
                        "kind": "composition_spec",
                        "media_type": "application/json",
                        "producer_stage": "stale-source-authority-test",
                    }
                ],
            )
        self.kernel.jobs.fail_attempt(
            admission.attempt_id,
            "EXPECTED_TEST_FAILURE",
            "stale composition source authority proof",
        )
        self.assertEqual(self.kernel.variants.variant(variant_id)["state"], "PROPOSED")

    def test_composition_registration_rejects_unqualified_persisted_route(self) -> None:
        revision = self.capture_revision()
        variant_id = self.kernel.variants.create(
            production_id=self.production_id,
            source_revision_id=revision.revision_id,
            intent=self._intent(),
        )
        self._materialize_variant_inputs(variant_id)
        legitimate = self.kernel.variant_pipeline.plan_composition(variant_id)
        self.assertIsNotNone(legitimate.job_id)
        legitimate_job = self.kernel.jobs.job(str(legitimate.job_id))
        voice_source = legitimate_job["spec"]["source_artifacts"][0]
        voice_artifact = self.kernel.artifacts.artifact(str(voice_source["artifact_id"]), verify_bytes=True)
        variant = self.kernel.variants.variant(variant_id)
        invented_route = dict(
            self.kernel.capabilities.router.qualify(
                "composition.compile",
                execution_mode="PRIVATE",
            )
        )
        invented_route["route_id"] = "invented_composition_route"
        invented_route["provider"] = "invented_provider"
        invented_route["model"] = "invented-composition-v1"
        semantic_inputs = {
            "variant_intent_hash": str(variant["intent_hash"]),
            "composition_preferences": {},
        }
        source_digests = {"voice": str(voice_artifact["object_digest"])}
        fingerprint = expected_fingerprint(
            "composition.compile",
            output_role="composition.primary",
            semantic_inputs=semantic_inputs,
            source_object_digests=source_digests,
            route=invented_route,
        )
        admission = self.kernel.jobs.admit(
            production_id=self.production_id,
            production_revision_id=revision.revision_id,
            variant_id=variant_id,
            job_class="ARTIFACT",
            job_type="COMPOSITION_COMPILE",
            semantic_capability="composition.compile",
            spec={
                "schema_version": 1,
                "execution_mode": "PRIVATE",
                "output_role": "composition.primary",
                "kind": "composition_spec",
                "media_type": "application/json",
                "filename": "composition.json",
                "semantic_inputs": semantic_inputs,
                "source_object_digests": source_digests,
                "source_artifacts": [voice_source],
            },
            route=invented_route,
            input_fingerprint=fingerprint,
            max_attempts=1,
        )
        composition = {
            "schema_version": 1,
            "variant_intent_hash": str(variant["intent_hash"]),
            "composition_preferences": {},
            "canvas": self.kernel.variants.canvas(self._intent()),
            "duration_ms": 1000,
            "tracks": [
                {"kind": "visual", "items": []},
                {"kind": "voice", "object_digest": str(voice_artifact["object_digest"])},
            ],
        }
        temp_dir = self.kernel.jobs.start_attempt(
            admission.attempt_id,
            "deterministic-artifact-provider",
        )
        (temp_dir / "composition.json").write_bytes(canonical_text(composition).encode("utf-8"))
        with self.assertRaisesRegex(InvalidArtifact, "qualified route"):
            self.kernel.jobs.complete_artifact_attempt(
                admission.attempt_id,
                [
                    {
                        "role": "composition.primary",
                        "path": "composition.json",
                        "kind": "composition_spec",
                        "media_type": "application/json",
                        "producer_stage": "route-authority-test",
                    }
                ],
            )
        self.kernel.jobs.fail_attempt(
            admission.attempt_id,
            "EXPECTED_TEST_FAILURE",
            "composition route authority proof",
        )

    def test_render_registration_rejects_duration_different_from_composition(self) -> None:
        revision = self.capture_revision()
        variant_id = self.kernel.variants.create(
            production_id=self.production_id,
            source_revision_id=revision.revision_id,
            intent=self._intent(),
        )
        self._materialize_variant_inputs(variant_id)
        composition_plan = self.kernel.variant_pipeline.plan_composition(variant_id)
        self.assertIsNotNone(composition_plan.job_id)
        self.kernel.variant_pipeline.execute_job(str(composition_plan.job_id))

        render_plan = self.kernel.variant_pipeline.plan_render(variant_id)
        self.assertIsNotNone(render_plan.job_id)
        render_job = self.kernel.jobs.job(str(render_plan.job_id))
        attempt_id = str(render_plan.attempt_id)
        temp_dir = self.kernel.jobs.start_attempt(
            attempt_id,
            "deterministic-media-renderer",
        )
        self.kernel.variant_pipeline._render_fixture(
            temp_dir / "render.mp4",
            {
                "canvas": self.kernel.variants.canvas(self._intent()),
                "duration_ms": 1500,
            },
            str(render_job["input_fingerprint"]),
        )
        with self.assertRaisesRegex(InvalidArtifact, "bound composition"):
            self.kernel.jobs.complete_artifact_attempt(
                attempt_id,
                [
                    {
                        "role": "render.primary",
                        "path": "render.mp4",
                        "kind": "rendered_video",
                        "media_type": "video/mp4",
                        "producer_stage": "duration-authority-test",
                    }
                ],
            )
        self.kernel.jobs.fail_attempt(
            attempt_id,
            "EXPECTED_TEST_FAILURE",
            "render duration authority proof",
        )
        self.assertEqual(self.kernel.variants.variant(variant_id)["state"], "PROPOSED")

    def test_render_registration_rejects_unqualified_persisted_route(self) -> None:
        revision = self.capture_revision()
        variant_id = self.kernel.variants.create(
            production_id=self.production_id,
            source_revision_id=revision.revision_id,
            intent=self._intent(),
        )
        self._materialize_variant_inputs(variant_id)
        composition_plan = self.kernel.variant_pipeline.plan_composition(variant_id)
        self.assertIsNotNone(composition_plan.job_id)
        composition_id = self.kernel.variant_pipeline.execute_job(str(composition_plan.job_id))
        composition_artifact = self.kernel.artifacts.artifact(composition_id, verify_bytes=True)
        composition = json.loads(self.kernel.artifacts.read_bytes(composition_id).decode("utf-8"))

        invented_route = dict(
            self.kernel.capabilities.router.qualify(
                "media.render",
                execution_mode="PRIVATE",
            )
        )
        invented_route["route_id"] = "invented_media_render_route"
        invented_route["provider"] = "invented_renderer"
        invented_route["model"] = "invented-render-v1"
        source_digests = {"composition_spec": str(composition_artifact["object_digest"])}
        fingerprint = expected_fingerprint(
            "media.render",
            output_role="render.primary",
            semantic_inputs={},
            source_object_digests=source_digests,
            route=invented_route,
        )
        admission = self.kernel.jobs.admit(
            production_id=self.production_id,
            production_revision_id=revision.revision_id,
            variant_id=variant_id,
            job_class="ARTIFACT",
            job_type="MEDIA_RENDER",
            semantic_capability="media.render",
            spec={
                "schema_version": 1,
                "execution_mode": "PRIVATE",
                "output_role": "render.primary",
                "kind": "rendered_video",
                "media_type": "video/mp4",
                "filename": "render.mp4",
                "semantic_inputs": {},
                "source_object_digests": source_digests,
                "source_artifacts": [
                    {"artifact_id": composition_id, "role": "composition_spec"},
                ],
            },
            route=invented_route,
            input_fingerprint=fingerprint,
            max_attempts=1,
        )
        temp_dir = self.kernel.jobs.start_attempt(
            admission.attempt_id,
            "deterministic-media-renderer",
        )
        self.kernel.variant_pipeline._render_fixture(
            temp_dir / "render.mp4",
            composition,
            fingerprint,
        )
        with self.assertRaisesRegex(InvalidArtifact, "qualified route"):
            self.kernel.jobs.complete_artifact_attempt(
                admission.attempt_id,
                [
                    {
                        "role": "render.primary",
                        "path": "render.mp4",
                        "kind": "rendered_video",
                        "media_type": "video/mp4",
                        "producer_stage": "route-authority-test",
                    }
                ],
            )
        self.kernel.jobs.fail_attempt(
            admission.attempt_id,
            "EXPECTED_TEST_FAILURE",
            "render route authority proof",
        )
        self.assertEqual(self.kernel.variants.variant(variant_id)["state"], "PROPOSED")

    def test_cover_registration_rejects_bytes_from_unbound_visual(self) -> None:
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=0,
            idempotency_key="cover-script",
            action="set_script",
            command_input={"text": "cover source proof"},
        )
        self.kernel.user.command(
            production_id=self.production_id,
            expected_state_version=1,
            idempotency_key="cover-visuals",
            action="set_visual_plan",
            command_input={
                "items": [
                    {"item_id": "scene-a", "description": "first source"},
                    {"item_id": "scene-b", "description": "second source"},
                ]
            },
        )
        revision = self.kernel.user.capture_revision(
            production_id=self.production_id,
            expected_state_version=2,
            idempotency_key="cover-capture",
        )
        for plan in self.kernel.derivations.plan_revision(revision.revision_id):
            if plan.job_id is not None and self.kernel.jobs.job(plan.job_id)["state"] == "QUEUED":
                self.kernel.derivations.execute_job(plan.job_id)
        reusable = {
            plan.output_role: plan
            for plan in self.kernel.derivations.plan_revision(revision.revision_id)
        }
        source_a = str(reusable["visual.scene-a"].artifact_id)
        source_b = str(reusable["visual.scene-b"].artifact_id)
        self.assertNotEqual(
            self.kernel.artifacts.artifact(source_a)["object_digest"],
            self.kernel.artifacts.artifact(source_b)["object_digest"],
        )

        cover_plan = self.kernel.variant_pipeline.plan_cover(source_a)
        self.assertIsNotNone(cover_plan.job_id)
        attempt_id = str(cover_plan.attempt_id)
        temp_dir = self.kernel.jobs.start_attempt(
            attempt_id,
            "deterministic-artifact-provider",
        )
        (temp_dir / "cover.png").write_bytes(self.kernel.artifacts.read_bytes(source_b))
        with self.assertRaisesRegex(InvalidArtifact, "bound selected visual"):
            self.kernel.jobs.complete_artifact_attempt(
                attempt_id,
                [
                    {
                        "role": "cover.primary",
                        "path": "cover.png",
                        "kind": "cover_image",
                        "media_type": "image/png",
                        "producer_stage": "cover-byte-authority-test",
                    }
                ],
            )
        self.kernel.jobs.fail_attempt(
            attempt_id,
            "EXPECTED_TEST_FAILURE",
            "cover byte authority proof",
        )
        with self.kernel.store.read() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM artifacts WHERE producer_job_id=?",
                (str(cover_plan.job_id),),
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_quicktime_mov_alias_cannot_be_registered_as_video_mp4(self) -> None:
        path = self.root / "quicktime.mov"
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
                "mov",
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
                producer_stage="mov-container-authority-test",
            )
