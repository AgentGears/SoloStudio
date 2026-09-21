from __future__ import annotations

from solostudio.kernel.derivations.fingerprints import expected_fingerprint
from solostudio.kernel.errors import InvalidArtifact
from solostudio.kernel.identity import canonical_text
from tests.integration.job_test_support import JobTestCase


class Slice8SourceAuthorityTests(JobTestCase):
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

    def _variant(self) -> tuple[str, str]:
        revision = self.capture_revision()
        variant_id = self.kernel.variants.create(
            production_id=self.production_id,
            source_revision_id=revision.revision_id,
            intent=self._intent(),
        )
        return revision.revision_id, variant_id

    def _materialize_variant_inputs(self, variant_id: str) -> None:
        plans = self.kernel.variant_pipeline.plan_inputs(variant_id)
        for plan in plans:
            if plan.job_id is None:
                continue
            job = self.kernel.jobs.job(plan.job_id)
            if job["state"] == "QUEUED":
                self.kernel.derivations.execute_job(plan.job_id)

    def test_render_registration_requires_bound_authoritative_composition(self) -> None:
        revision_id, variant_id = self._variant()
        route = self.kernel.capabilities.router.qualify("media.render", execution_mode="PRIVATE")
        pretend_digest = "a" * 64
        fingerprint = expected_fingerprint(
            "media.render",
            output_role="render.primary",
            semantic_inputs={},
            source_object_digests={"composition_spec": pretend_digest},
            route=route,
        )
        admission = self.kernel.jobs.admit(
            production_id=self.production_id,
            production_revision_id=revision_id,
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
                "source_object_digests": {"composition_spec": pretend_digest},
                "source_artifacts": [],
            },
            route=route,
            input_fingerprint=fingerprint,
            max_attempts=1,
        )
        temp_dir = self.kernel.jobs.start_attempt(
            admission.attempt_id,
            "deterministic-media-renderer",
        )
        self.kernel.variant_pipeline._render_fixture(
            temp_dir / "render.mp4",
            {
                "canvas": self.kernel.variants.canvas(self._intent()),
                "duration_ms": 1000,
            },
            fingerprint,
        )
        with self.assertRaises(InvalidArtifact):
            self.kernel.jobs.complete_artifact_attempt(
                admission.attempt_id,
                [
                    {
                        "role": "render.primary",
                        "path": "render.mp4",
                        "kind": "rendered_video",
                        "media_type": "video/mp4",
                        "producer_stage": "source-authority-test",
                    }
                ],
            )
        self.kernel.jobs.fail_attempt(
            admission.attempt_id,
            "EXPECTED_TEST_FAILURE",
            "render source authority proof",
        )
        self.assertEqual(self.kernel.variants.variant(variant_id)["state"], "PROPOSED")
        with self.kernel.store.read() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM artifacts WHERE variant_id=? AND kind='rendered_video'",
                (variant_id,),
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_composition_registration_rejects_track_digest_not_bound_to_source(self) -> None:
        _revision_id, variant_id = self._variant()
        self._materialize_variant_inputs(variant_id)
        plan = self.kernel.variant_pipeline.plan_composition(variant_id)
        self.assertIsNotNone(plan.job_id)
        job = self.kernel.jobs.job(str(plan.job_id))
        source_digests = job["spec"]["source_object_digests"]
        actual_voice_digest = str(source_digests["voice"])
        wrong_voice_digest = "0" * 64 if actual_voice_digest != "0" * 64 else "f" * 64
        variant = self.kernel.variants.variant(variant_id)
        malicious_composition = {
            "schema_version": 1,
            "variant_intent_hash": str(variant["intent_hash"]),
            "composition_preferences": {},
            "canvas": self.kernel.variants.canvas(self._intent()),
            "duration_ms": 1000,
            "tracks": [
                {"kind": "visual", "items": []},
                {"kind": "voice", "object_digest": wrong_voice_digest},
            ],
        }
        attempt_id = str(plan.attempt_id)
        temp_dir = self.kernel.jobs.start_attempt(
            attempt_id,
            "deterministic-artifact-provider",
        )
        (temp_dir / "composition.json").write_bytes(
            canonical_text(malicious_composition).encode("utf-8")
        )
        with self.assertRaises(InvalidArtifact):
            self.kernel.jobs.complete_artifact_attempt(
                attempt_id,
                [
                    {
                        "role": "composition.primary",
                        "path": "composition.json",
                        "kind": "composition_spec",
                        "media_type": "application/json",
                        "producer_stage": "source-authority-test",
                    }
                ],
            )
        self.kernel.jobs.fail_attempt(
            attempt_id,
            "EXPECTED_TEST_FAILURE",
            "composition source authority proof",
        )
        with self.kernel.store.read() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM artifacts WHERE variant_id=? AND kind='composition_spec'",
                (variant_id,),
            ).fetchone()[0]
        self.assertEqual(count, 0)
        self.assertEqual(self.kernel.variants.variant(variant_id)["state"], "PROPOSED")
