from __future__ import annotations

import io
import struct
import wave
import zlib

from solostudio.kernel.errors import InvalidArtifact
from tests.integration.job_test_support import JobTestCase


class DerivationValidatorEdgeTests(JobTestCase):
    def test_wav_validator_rejects_truncated_frame_payload(self) -> None:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(1)
            wav.setframerate(8000)
            wav.writeframes(b"\x80" * 32)
        truncated = buffer.getvalue()[:-1]
        with self.assertRaises(InvalidArtifact):
            self.kernel.artifacts.prepare_bytes(
                truncated,
                kind="voice_audio",
                media_type="audio/wav",
                producer_stage="validator-edge-proof",
            )

    def test_webvtt_validator_rejects_invalid_timing_line(self) -> None:
        payload = b"WEBVTT\n\n00:bogus --> 00:05.000\ncaption\n"
        with self.assertRaises(InvalidArtifact):
            self.kernel.artifacts.prepare_bytes(
                payload,
                kind="caption_track",
                media_type="text/vtt; charset=utf-8",
                producer_stage="validator-edge-proof",
            )

    def test_png_validator_rejects_illegal_scanline_filter(self) -> None:
        raw = bytes([5, 0, 0, 0])
        payload = b"".join(
            [
                b"\x89PNG\r\n\x1a\n",
                _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)),
                _chunk(b"IDAT", zlib.compress(raw)),
                _chunk(b"IEND", b""),
            ]
        )
        with self.assertRaises(InvalidArtifact):
            self.kernel.artifacts.prepare_bytes(
                payload,
                kind="visual_image",
                media_type="image/png",
                producer_stage="validator-edge-proof",
            )


def _chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind)
    crc = zlib.crc32(data, crc) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)
