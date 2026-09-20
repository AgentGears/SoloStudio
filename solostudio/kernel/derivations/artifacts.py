from __future__ import annotations

import io
import json
import re
import struct
import subprocess
import tempfile
import wave
import zlib
from pathlib import Path
from typing import Any, Iterable

from solostudio.kernel.artifacts import ArtifactService, PreparedArtifact
from solostudio.kernel.errors import (
    ArtifactDigestMismatch,
    InvalidArtifact,
    MissingRetainedArtifact,
    NotFound,
)
from solostudio.kernel.identity import canonical_text


_VTT_TIMING = re.compile(
    r"^(?P<start>(?:\d+:)?[0-5]\d:[0-5]\d\.\d{3})\s+-->\s+"
    r"(?P<end>(?:\d+:)?[0-5]\d:[0-5]\d\.\d{3})(?:\s+.*)?$"
)


class DerivationArtifactService(ArtifactService):
    """Artifact authority extensions required by dependency-aware derivations."""

    def register_prepared_in_tx(
        self,
        db: Any,
        prepared: PreparedArtifact,
        *,
        production_id: str,
        production_revision_id: str | None = None,
        variant_id: str | None = None,
        producer_job_id: str | None = None,
        producer_attempt_id: str | None = None,
        dependencies: Iterable[tuple[str, str]] = (),
    ) -> str:
        resolved_dependencies = tuple(dependencies)
        if producer_job_id is not None and not resolved_dependencies:
            job = db.execute(
                "SELECT spec_json FROM job_specs WHERE id = ?",
                (producer_job_id,),
            ).fetchone()
            if job is not None:
                spec = json.loads(str(job["spec_json"]))
                source_artifacts = spec.get("source_artifacts", [])
                if not isinstance(source_artifacts, list):
                    raise InvalidArtifact("artifact job source_artifacts must be a list")
                derived: list[tuple[str, str]] = []
                for source in source_artifacts:
                    if not isinstance(source, dict):
                        raise InvalidArtifact("artifact job source_artifacts entries must be objects")
                    artifact_id = source.get("artifact_id")
                    role = source.get("role")
                    if not isinstance(artifact_id, str) or not artifact_id:
                        raise InvalidArtifact("artifact job dependency requires artifact_id")
                    if not isinstance(role, str) or not role:
                        raise InvalidArtifact("artifact job dependency requires role")
                    derived.append((artifact_id, role))
                resolved_dependencies = tuple(derived)

        return super().register_prepared_in_tx(
            db,
            prepared,
            production_id=production_id,
            production_revision_id=production_revision_id,
            variant_id=variant_id,
            producer_job_id=producer_job_id,
            producer_attempt_id=producer_attempt_id,
            dependencies=resolved_dependencies,
        )

    def captured_source(
        self,
        revision_id: str,
        kind: str,
        *,
        expected_digest: str,
    ) -> dict[str, Any]:
        with self.store.read() as db:
            ids = [
                str(row["id"])
                for row in db.execute(
                    """
                    SELECT id
                    FROM artifacts
                    WHERE production_revision_id = ?
                      AND variant_id IS NULL
                      AND kind = ?
                      AND object_digest = ?
                      AND producer_stage = 'revision_capture'
                      AND producer_job_id IS NULL
                      AND producer_attempt_id IS NULL
                    ORDER BY id
                    """,
                    (revision_id, kind, expected_digest),
                )
            ]
        if not ids:
            raise NotFound(f"captured {kind} source not found for revision: {revision_id}")
        for artifact_id in ids:
            try:
                artifact = self.artifact(artifact_id, verify_bytes=True)
                payload = self.read_bytes(artifact_id)
                self._validate(str(artifact["kind"]), str(artifact["media_type"]), payload)
                return artifact
            except (InvalidArtifact, MissingRetainedArtifact, ArtifactDigestMismatch):
                continue
        raise InvalidArtifact(f"no valid captured {kind} source remains for revision: {revision_id}")

    def find_reusable(
        self,
        production_id: str,
        kind: str,
        expected_fingerprint: str,
    ) -> dict[str, Any] | None:
        with self.store.read() as db:
            ids = [
                str(row["id"])
                for row in db.execute(
                    """
                    SELECT id
                    FROM artifacts
                    WHERE production_id = ?
                      AND kind = ?
                      AND input_fingerprint = ?
                    ORDER BY created_at, id
                    """,
                    (production_id, kind, expected_fingerprint),
                )
            ]
        for artifact_id in ids:
            try:
                artifact = self.artifact(artifact_id, verify_bytes=True)
                payload = self.read_bytes(artifact_id)
                self._validate(str(artifact["kind"]), str(artifact["media_type"]), payload)
                return artifact
            except (InvalidArtifact, MissingRetainedArtifact, ArtifactDigestMismatch):
                continue
        return None

    def dependencies(self, artifact_id: str) -> list[dict[str, str]]:
        with self.store.read() as db:
            if not db.execute("SELECT 1 FROM artifacts WHERE id = ?", (artifact_id,)).fetchone():
                raise NotFound(f"artifact not found: {artifact_id}")
            return [
                {
                    "source_artifact_id": str(row["source_artifact_id"]),
                    "role": str(row["dependency_role"]),
                }
                for row in db.execute(
                    """
                    SELECT source_artifact_id,dependency_role
                    FROM artifact_dependencies
                    WHERE artifact_id = ?
                    ORDER BY dependency_role, source_artifact_id
                    """,
                    (artifact_id,),
                )
            ]

    @staticmethod
    def _validate(kind: str, media_type: str, payload: bytes) -> None:
        ArtifactService._validate(kind, media_type, payload)
        if kind == "voice_audio":
            if media_type != "audio/wav":
                raise InvalidArtifact("voice_audio requires WAV media type")
            try:
                with wave.open(io.BytesIO(payload), "rb") as wav:
                    channels = wav.getnchannels()
                    sample_width = wav.getsampwidth()
                    frame_rate = wav.getframerate()
                    frame_count = wav.getnframes()
                    if channels < 1 or sample_width < 1 or frame_rate < 1 or frame_count < 1:
                        raise InvalidArtifact("voice_audio WAV stream is empty or malformed")
                    frames = wav.readframes(frame_count)
                    expected_bytes = frame_count * channels * sample_width
                    if len(frames) != expected_bytes:
                        raise InvalidArtifact("voice_audio WAV frame payload is truncated")
            except (wave.Error, EOFError) as exc:
                raise InvalidArtifact("voice_audio is not a decodable WAV payload") from exc
            return
        if kind == "caption_track":
            if media_type != "text/vtt; charset=utf-8":
                raise InvalidArtifact("caption_track requires UTF-8 WebVTT media type")
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise InvalidArtifact("caption_track bytes are not valid UTF-8") from exc
            if not _valid_webvtt(text):
                raise InvalidArtifact("caption_track is not a structurally valid WebVTT payload")
            return
        if kind in {"visual_image", "cover_image"}:
            if media_type != "image/png":
                raise InvalidArtifact(f"{kind} requires PNG media type")
            if not _valid_png(payload):
                raise InvalidArtifact(f"{kind} is not a structurally valid PNG payload")
            return
        if kind == "composition_spec":
            if media_type != "application/json":
                raise InvalidArtifact("composition_spec requires JSON media type")
            try:
                value = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise InvalidArtifact("composition_spec is not valid UTF-8 JSON") from exc
            if canonical_text(value).encode("utf-8") != payload:
                raise InvalidArtifact("composition_spec bytes must use canonical JSON")
            if not _valid_composition_spec(value):
                raise InvalidArtifact("composition_spec structure is invalid")
            return
        if kind == "rendered_video":
            if media_type != "video/mp4":
                raise InvalidArtifact("rendered_video requires MP4 media type")
            if len(payload) < 1024:
                raise InvalidArtifact("rendered_video is below the M0 minimum byte size")
            with tempfile.NamedTemporaryFile(suffix=".mp4") as handle:
                handle.write(payload)
                handle.flush()
                probe_media_file(Path(handle.name))
            return


def probe_media_file(path: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InvalidArtifact("media container validation could not run") from exc
    if completed.returncode != 0:
        raise InvalidArtifact("rendered media container does not decode")
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise InvalidArtifact("media probe result is invalid") from exc
    streams = data.get("streams")
    format_info = data.get("format")
    if not isinstance(streams, list) or not isinstance(format_info, dict):
        raise InvalidArtifact("media probe result is incomplete")
    video_streams = [stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "audio"]
    if not video_streams:
        raise InvalidArtifact("rendered media has no video stream")
    if not audio_streams:
        raise InvalidArtifact("rendered media has no audio stream")
    video = video_streams[0]
    try:
        width = int(video.get("width"))
        height = int(video.get("height"))
        duration_seconds = float(format_info.get("duration"))
        byte_size = int(format_info.get("size", path.stat().st_size))
    except (TypeError, ValueError, OSError) as exc:
        raise InvalidArtifact("media probe dimensions, duration, or size are invalid") from exc
    if width < 1 or height < 1 or duration_seconds <= 0 or byte_size <= 0:
        raise InvalidArtifact("media probe reported invalid dimensions, duration, or size")
    return {
        "validator": "ffprobe-v1",
        "width": width,
        "height": height,
        "duration_ms": int(round(duration_seconds * 1000)),
        "byte_size": byte_size,
        "video_streams": len(video_streams),
        "audio_streams": len(audio_streams),
    }


def _valid_composition_spec(value: Any) -> bool:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return False
    intent_hash = value.get("variant_intent_hash")
    canvas = value.get("canvas")
    duration_ms = value.get("duration_ms")
    tracks = value.get("tracks")
    if not _is_sha256(intent_hash) or not isinstance(canvas, dict) or type(duration_ms) is not int or duration_ms < 1:
        return False
    if set(canvas) != {"aspect_ratio", "width", "height", "fps"}:
        return False
    if (
        not isinstance(canvas.get("aspect_ratio"), str)
        or type(canvas.get("width")) is not int
        or type(canvas.get("height")) is not int
        or type(canvas.get("fps")) is not int
        or canvas["width"] < 1
        or canvas["height"] < 1
        or canvas["fps"] < 1
    ):
        return False
    if not isinstance(tracks, list):
        return False
    kinds = [track.get("kind") for track in tracks if isinstance(track, dict)]
    if len(kinds) != len(tracks) or "voice" not in kinds or "visual" not in kinds:
        return False
    for track in tracks:
        if track.get("kind") == "visual":
            items = track.get("items")
            if not isinstance(items, list):
                return False
            for item in items:
                if not isinstance(item, dict) or not _is_sha256(item.get("object_digest")):
                    return False
        elif track.get("kind") in {"voice", "captions"}:
            if not _is_sha256(track.get("object_digest")):
                return False
        else:
            return False
    return True


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _valid_webvtt(text: str) -> bool:
    if not text.startswith("WEBVTT\n"):
        return False
    saw_cue = False
    for line in text.splitlines()[1:]:
        if "-->" not in line:
            continue
        match = _VTT_TIMING.fullmatch(line.strip())
        if match is None:
            return False
        start = _vtt_milliseconds(match.group("start"))
        end = _vtt_milliseconds(match.group("end"))
        if start is None or end is None or end <= start:
            return False
        saw_cue = True
    return saw_cue


def _vtt_milliseconds(value: str) -> int | None:
    clock, millis = value.rsplit(".", 1)
    if len(millis) != 3 or not millis.isdigit():
        return None
    parts = clock.split(":")
    if len(parts) == 2:
        hours = 0
        minutes_text, seconds_text = parts
    elif len(parts) == 3:
        hours_text, minutes_text, seconds_text = parts
        if not hours_text.isdigit():
            return None
        hours = int(hours_text)
    else:
        return None
    if not minutes_text.isdigit() or not seconds_text.isdigit():
        return None
    minutes = int(minutes_text)
    seconds = int(seconds_text)
    if minutes > 59 or seconds > 59:
        return None
    return (((hours * 60) + minutes) * 60 + seconds) * 1000 + int(millis)


def _valid_png(payload: bytes) -> bool:
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    offset = 8
    width = height = None
    idat = bytearray()
    saw_ihdr = False
    while offset + 12 <= len(payload):
        length = struct.unpack(">I", payload[offset:offset + 4])[0]
        chunk_type = payload[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(payload):
            return False
        data = payload[offset + 8:offset + 8 + length]
        expected_crc = struct.unpack(">I", payload[offset + 8 + length:end])[0]
        actual_crc = zlib.crc32(chunk_type)
        actual_crc = zlib.crc32(data, actual_crc) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            return False
        if not saw_ihdr:
            if chunk_type != b"IHDR" or length != 13:
                return False
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", data
            )
            if (
                width < 1
                or height < 1
                or bit_depth != 8
                or color_type != 2
                or compression != 0
                or filtering != 0
                or interlace != 0
            ):
                return False
            saw_ihdr = True
        elif chunk_type == b"IDAT":
            idat.extend(data)
        elif chunk_type == b"IEND":
            if length != 0 or not idat or width is None or height is None or end != len(payload):
                return False
            try:
                raw = zlib.decompress(bytes(idat))
            except zlib.error:
                return False
            stride = 1 + width * 3
            if len(raw) != height * stride:
                return False
            return all(raw[row * stride] <= 4 for row in range(height))
        offset = end
    return False
