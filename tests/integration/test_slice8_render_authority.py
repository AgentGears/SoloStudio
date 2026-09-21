from __future__ import annotations

from solostudio.kernel.errors import InvalidArtifact
from tests.integration.job_test_support import JobTestCase


class Slice8RenderAuthorityTests(JobTestCase):
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

    def test_non_media_render_job_cannot_register_render_or_make_variant_ready(self) -> None:
        revision_id, variant_id = self._variant()
        route = self.kernel.capabilities.router.qualify("media.render", execution_mode="PRIVATE")
        admission = self.kernel.jobs.admit(
            production_id=self.production_id,
            production_revision_id=revision_id,
            variant_id=variant_id,
            job_class="ARTIFACT",
            job_type="NOT_MEDIA_RENDER",
            semantic_capability="not.media.render",
            spec={"schema_version": 1},
            route=route,
            input_fingerprint="a" * 64,
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
            "a" * 64,
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
                        "producer_stage": "authority-test",
                    }
                ],
            )
        self.kernel.jobs.fail_attempt(admission.attempt_id, "EXPECTED_TEST_FAILURE", "authority proof")
        self.assertEqual(self.kernel.variants.variant(variant_id)["state"], "PROPOSED")
        with self.kernel.store.read() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM artifacts WHERE variant_id=? AND kind='rendered_video'",
                (variant_id,),
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_render_validator_result_is_derived_from_exact_bytes(self) -> None:
        _revision_id, _variant_id = self._variant()
        attempt_root = self.root / "validator-fixture"
        attempt_root.mkdir()
        path = attempt_root / "render.mp4"
        self.kernel.variant_pipeline._render_fixture(
            path,
            {
                "canvas": self.kernel.variants.canvas(self._intent()),
                "duration_ms": 1000,
            },
            "b" * 64,
        )
        prepared = self.kernel.artifacts.prepare_bytes(
            path.read_bytes(),
            kind="rendered_video",
            media_type="video/mp4",
            producer_stage="validator-authority-test",
            metadata={"validator_result": {"width": 1, "height": 1}},
        )
        probe = prepared.metadata["validator_result"]
        self.assertEqual(probe["width"], 1080)
        self.assertEqual(probe["height"], 1920)
        self.assertGreaterEqual(probe["duration_ms"], 1000)
        self.assertGreaterEqual(probe["video_streams"], 1)
        self.assertGreaterEqual(probe["audio_streams"], 1)
