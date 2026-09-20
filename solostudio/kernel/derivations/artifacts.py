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

    def prepare_bytes(
        self,
        payload: bytes,
        *,
        kind: str,
        media_type: str,
        producer_stage: str,
        input_fingerprint: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PreparedArtifact:
        if kind != "rendered_video":
            return super().prepare_bytes(
                payload,
                kind=kind,
                media_type=media_type,
                producer_stage=producer_stage,
                input_fingerprint=input_fingerprint,
                metadata=metadata,
            )
        if media_type != "video/mp4":
            raise InvalidArtifact("rendered_video requires MP4 media type")
        if len(payload) < 1024:
            raise InvalidArtifact("rendered_video is below the M0 minimum byte size")
        probe = _probe_media_bytes(payload)
        authoritative_metadata = dict(metadata or {})
        authoritative_metadata["validator_result"] = probe
        obj = self.objects.promote_bytes(payload)
        return PreparedArtifact(
            obj,
            kind,
            media_type,
            producer_stage,
            input_fingerprint,
            authoritative_metadata,
        )

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
        if prepared.kind == "rendered_video":
            self._validate_render_registration(
                db,
                prepared,
                production_id=production_id,
                production_revision_id=production_revision_id,
                variant_id=variant_id,
                producer_job_id=producer_job_id,
            )

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

    def _validate_render_registration(
        self,
        db: Any,
        prepared: PreparedArtifact,
        *,
        production_id: str,
        production_revision_id: str | None,
        variant_id: str | None,
        producer_job_id: str | None,
    ) -> None:
        if variant_id is None or production_revision_id is None or producer_job_id is None:
            raise InvalidArtifact("rendered_video requires variant, revision, and producer JobSpec lineage")
        job = db.execute(
            """
            SELECT production_id,production_revision_id,variant_id,job_class,job_type,semantic_capability
            FROM job_specs WHERE id=?
            """,
            (producer_job_id,),
        ).fetchone()
        if not job:
            raise NotFound(f"job not found: {producer_job_id}")
        if (
            str(job["production_id"]) != production_id
            or job["production_revision_id"] != production_revision_id
            or job["variant_id"] != variant_id
            or job["job_class"] != "ARTIFACT"
            or job["job_type"] != "MEDIA_RENDER"
            or job["semantic_capability"] != "media.render"
        ):
            raise InvalidArtifact("rendered_video producer job does not hold media-render variant authority")
        variant = db.execute(
            "SELECT production_id,source_revision_id,intent_json FROM delivery_variants WHERE id=?",
            (variant_id,),
        ).fetchone()
        if not variant:
            raise NotFound(f"variant not found: {variant_id}")
        if (
            str(variant["production_id"]) != production_id
            or str(variant["source_revision_id"]) != production_revision_id
        ):
            raise InvalidArtifact("rendered_video variant lineage is inconsistent")
        try:
            intent = json.loads(str(variant["intent_json"]))
        except json.JSONDecodeError as exc:
            raise InvalidArtifact("rendered_video variant intent is unreadable") from exc
        payload = self.objects.read_bytes(
            prepared.object_record.digest_sha256,
            prepared.object_record.byte_size,
            prepared.object_record.object_relpath,
        )
        authoritative_probe = _probe_media_bytes(payload)
        recorded_probe = (prepared.metadata or {}).get("validator_result")
        if recorded_probe != authoritative_probe:
            raise InvalidArtifact("rendered_video validator result is not authoritative for retained bytes")
        _validate_render_probe_for_intent(authoritative_probe, intent)

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
            _probe_media_bytes(payload)
            return


def _probe_media_bytes(payload: bytes) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temp_dir:
        media_path = Path(temp_dir) / "render.mp4"
        media_path.write_bytes(payload)
        return probe_media_file(media_path)


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


def _validate_render_probe_for_intent(probe: dict[str, Any], intent: Any) -> None:
    if not isinstance(intent, dict):
        raise InvalidArtifact("rendered_video variant intent is invalid")
    aspect = intent.get("aspect_ratio")
    if aspect == "9:16":
        expected_width, expected_height = 1080, 1920
    elif aspect == "1:1":
        expected_width, expected_height = 1080, 1080
    else:
        raise InvalidArtifact("rendered_video variant aspect ratio is unsupported")
    duration_min = intent.get("duration_min_ms")
    duration_max = intent.get("duration_max_ms")
    if type(duration_min) is not int or type(duration_max) is not int:
        raise InvalidArtifact("rendered_video variant duration bounds are invalid")
    if probe.get("width") != expected_width or probe.get("height") != expected_height:
        raise InvalidArtifact("rendered_video dimensions do not match bound variant")
    duration = probe.get("duration_ms")
    if type(duration) is not int or not (duration_min <= duration <= duration_max) or duration > 90_000:
        raise InvalidArtifact("rendered_video duration does not match bound variant")
    if type(probe.get("video_streams")) is not int or probe["video_streams"] < 1:
        raise InvalidArtifact("rendered_video is missing required video stream")
    if type(probe.get("audio_streams")) is not int or probe["audio_streams"] < 1:
        raise InvalidArtifact("rendered_video is missing required audio stream")


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
