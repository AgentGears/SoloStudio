from __future__ import annotations

import io
import json
import subprocess
import wave

from solostudio.kernel.derivations.fingerprints import expected_fingerprint
from solostudio.kernel.errors import InvalidArtifact
from solostudio.kernel.identity import canonical_text
from tests.integration.job_test_support import JobTestCase


class Slice8FinalExactHeadFindingTests(JobTestCase):
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

    @staticmethod
    def _valid_wav() -> bytes:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(1)
            wav.setframerate(8000)
            wav.writeframes(b"\x80" * 800)
        return buffer.getvalue()

    def _voice_requirement(self, revision_id: str):
        revision = self.kernel.productions.revision(revision_id)
        payload = json.loads(str(revision["canonical_json"]))
        return next(
            requirement
            for requirement in self.kernel.derivations._requirements(
                revision_id,
                payload,
                execution_mode="PRIVATE",
            )
            if requirement.output_role == "voice.primary"
        )

    def _materialize_composition(self, variant_id: str) -> str:
        for plan in self.kernel.variant_pipeline.plan_inputs(variant_id):
            if plan.job_id is not None and self.kernel.jobs.job(plan.job_id)["state"] == "QUEUED":
                self.kernel.derivations.execute_job(plan.job_id)
        composition_plan = self.kernel.variant_pipeline.plan_composition(variant_id)
        self.assertIsNotNone(composition_plan.job_id)
        return self.kernel.variant_pipeline.execute_job(str(composition_plan.job_id))

    def test_composition_rejects_fingerprint_only_forged_derivation_source(self) -> None:
        revision = self.capture_revision()
        requirement = self._voice_requirement(revision.revision_id)
        forged_id = self.kernel.artifacts.promote_and_register(
            self._valid_wav(),
            production_id=self.production_id,
            production_revision_id=revision.revision_id,
            kind="voice_audio",
            media_type="audio/wav",
            producer_stage="forged-import",
            input_fingerprint=requirement.input_fingerprint,
        )
        forged = self.kernel.artifacts.artifact(forged_id, verify_bytes=True)
        self.assertIsNone(forged["producer_job_id"])
        self.assertIsNone(forged["producer_attempt_id"])

        variant_id = self.kernel.variants.create(
            production_id=self.production_id,
            source_revision_id=revision.revision_id,
            intent=self._intent(),
        )
        variant = self.kernel.variants.variant(variant_id)
        revision_payload = json.loads(
            str(self.kernel.productions.revision(revision.revision_id)["canonical_json"])
        )
        route = self.kernel.capabilities.router.qualify(
            "composition.compile",
            execution_mode="PRIVATE",
        )
        semantic_inputs = {
            "variant_intent_hash": str(variant["intent_hash"]),
            "composition_preferences": revision_payload["composition_preferences"],
        }
        source_digests = {"voice": str(forged["object_digest"])}
        fingerprint = expected_fingerprint(
            "composition.compile",
            output_role="composition.primary",
            semantic_inputs=semantic_inputs,
            source_object_digests=source_digests,
            route=route,
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
                "source_artifacts": [{"artifact_id": forged_id, "role": "voice"}],
            },
            route=route,
            input_fingerprint=fingerprint,
            max_attempts=1,
        )
        composition = {
            "schema_version": 1,
            "variant_intent_hash": str(variant["intent_hash"]),
            "composition_preferences": revision_payload["composition_preferences"],
            "canvas": self.kernel.variants.canvas(self._intent()),
            "duration_ms": 1000,
            "tracks": [
                {"kind": "visual", "items": []},
                {"kind": "voice", "object_digest": str(forged["object_digest"])},
            ],
        }
        temp_dir = self.kernel.jobs.start_attempt(
            admission.attempt_id,
            "deterministic-artifact-provider",
        )
        (temp_dir / "composition.json").write_bytes(
            canonical_text(composition).encode("utf-8")
        )
        with self.assertRaisesRegex(InvalidArtifact, "authoritative producer"):
            self.kernel.jobs.complete_artifact_attempt(
                admission.attempt_id,
                [
                    {
                        "role": "composition.primary",
                        "path": "composition.json",
                        "kind": "composition_spec",
                        "media_type": "application/json",
                        "producer_stage": "forged-source-authority-test",
                    }
                ],
            )
        self.kernel.jobs.fail_attempt(
            admission.attempt_id,
            "EXPECTED_TEST_FAILURE",
            "fingerprint-only derivation source must not hold composition authority",
        )

        plans = self.kernel.variant_pipeline.plan_inputs(variant_id)
        voice = next(plan for plan in plans if plan.output_role == "voice.primary")
        self.assertNotEqual(voice.artifact_id, forged_id)
        self.assertIsNotNone(voice.job_id)

    def test_render_registration_rejects_frame_rate_not_bound_to_composition(self) -> None:
        revision = self.capture_revision()
        variant_id = self.kernel.variants.create(
            production_id=self.production_id,
            source_revision_id=revision.revision_id,
            intent=self._intent(),
        )
        composition_id = self._materialize_composition(variant_id)
        composition = json.loads(self.kernel.artifacts.read_bytes(composition_id).decode("utf-8"))
        canvas = composition["canvas"]
        duration_seconds = f"{int(composition['duration_ms']) / 1000:.3f}"

        render_plan = self.kernel.variant_pipeline.plan_render(variant_id)
        self.assertIsNotNone(render_plan.job_id)
        attempt_id = str(render_plan.attempt_id)
        temp_dir = self.kernel.jobs.start_attempt(
            attempt_id,
            "deterministic-media-renderer",
        )
        output_path = temp_dir / "render.mp4"
        completed = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c=black:s={int(canvas['width'])}x{int(canvas['height'])}:r=1",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000",
                "-t",
                duration_seconds,
                "-map_metadata",
                "-1",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-movflags",
                "+faststart",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

        with self.assertRaisesRegex(InvalidArtifact, "frame rate"):
            self.kernel.jobs.complete_artifact_attempt(
                attempt_id,
                [
                    {
                        "role": "render.primary",
                        "path": "render.mp4",
                        "kind": "rendered_video",
                        "media_type": "video/mp4",
                        "producer_stage": "frame-rate-authority-test",
                    }
                ],
            )
        self.kernel.jobs.fail_attempt(
            attempt_id,
            "EXPECTED_TEST_FAILURE",
            "render frame rate must match composition canvas",
        )
        self.assertEqual(self.kernel.variants.variant(variant_id)["state"], "PROPOSED")
