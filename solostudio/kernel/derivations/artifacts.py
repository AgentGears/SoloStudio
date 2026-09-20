from __future__ import annotations

import io
import json
import struct
import wave
import zlib
from typing import Any, Iterable

from solostudio.kernel.artifacts import ArtifactService, PreparedArtifact
from solostudio.kernel.errors import (
    ArtifactDigestMismatch,
    InvalidArtifact,
    MissingRetainedArtifact,
    NotFound,
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
                    if (
                        wav.getnchannels() < 1
                        or wav.getsampwidth() < 1
                        or wav.getframerate() < 1
                        or wav.getnframes() < 1
                    ):
                        raise InvalidArtifact("voice_audio WAV stream is empty or malformed")
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
            if not text.startswith("WEBVTT\n") or "-->" not in text:
                raise InvalidArtifact("caption_track is not a structurally valid WebVTT payload")
            return
        if kind == "visual_image":
            if media_type != "image/png":
                raise InvalidArtifact("visual_image requires PNG media type")
            if not _valid_png(payload):
                raise InvalidArtifact("visual_image is not a structurally valid PNG payload")


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
            return len(raw) == height * (1 + width * 3)
        offset = end
    return False
