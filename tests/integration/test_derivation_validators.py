from __future__ import annotations

from solostudio.kernel.errors import InvalidArtifact
from tests.integration.job_test_support import JobTestCase


class DerivationValidatorTests(JobTestCase):
    def test_derived_media_validators_reject_structural_fakes(self) -> None:
        invalid_cases = (
            ("voice_audio", "audio/wav", b"RIFF" + b"x" * 40),
            ("caption_track", "text/vtt; charset=utf-8", b"WEBVTT\n\nnot-a-cue\n"),
            ("visual_image", "image/png", b"\x89PNG\r\n\x1a\n" + b"x" * 32),
        )
        for kind, media_type, payload in invalid_cases:
            with self.subTest(kind=kind), self.assertRaises(InvalidArtifact):
                self.kernel.artifacts.prepare_bytes(
                    payload,
                    kind=kind,
                    media_type=media_type,
                    producer_stage="validator-proof",
                )
