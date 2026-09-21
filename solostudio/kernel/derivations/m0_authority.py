from __future__ import annotations

import hashlib
import json
import math
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from solostudio.kernel.artifacts import PreparedArtifact
from solostudio.kernel.derivations.variant_artifacts import (
    VariantArtifactAuthorityService,
    _captured_definition_identity,
)
from solostudio.kernel.errors import (
    ArtifactDigestMismatch,
    InvalidArtifact,
    MissingRetainedArtifact,
    NotFound,
)
from solostudio.kernel.identity import canonical_hash, canonical_text


_DERIVATION_AUTHORITY = {
    "voice_audio": {
        "capability": "speech.synthesize",
        "job_type": "VOICE_SYNTHESIZE",
        "output_role": "voice.primary",
        "media_type": "audio/wav",
        "filename": "voice.wav",
    },
    "caption_track": {
        "capability": "captions.generate",
        "job_type": "CAPTIONS_GENERATE",
        "output_role": "captions.primary",
        "media_type": "text/vtt; charset=utf-8",
        "filename": "captions.vtt",
    },
    "visual_image": {
        "capability": "image.generate",
        "job_type": "VISUAL_IMAGE_GENERATE",
        "output_role": None,
        "media_type": "image/png",
        "filename": None,
    },
}


class M0ArtifactAuthorityService(VariantArtifactAuthorityService):
    """Final M0 Artifact authority fences shared by revision and variant planners."""

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
        prepared = super().prepare_bytes(
            payload,
            kind=kind,
            media_type=media_type,
            producer_stage=producer_stage,
            input_fingerprint=input_fingerprint,
            metadata=metadata,
        )
        if kind != "rendered_video" or media_type != "video/mp4":
            return prepared
        authoritative_metadata = dict(prepared.metadata or {})
        authoritative_metadata["frame_rate_result"] = _probe_video_frame_rate(payload)
        return PreparedArtifact(
            prepared.object_record,
            prepared.kind,
            prepared.media_type,
            prepared.producer_stage,
            prepared.input_fingerprint,
            authoritative_metadata,
        )

    def find_reusable(
        self,
        production_id: str,
        kind: str,
        expected_fingerprint: str,
    ) -> dict[str, Any] | None:
        """Return only reusable revision-scoped Artifacts with valid retained/provenance authority."""
        with self.store.read() as db:
            ids = [
                str(row["id"])
                for row in db.execute(
                    """
                    SELECT id
                    FROM artifacts
                    WHERE production_id = ?
                      AND variant_id IS NULL
                      AND kind = ?
                      AND input_fingerprint = ?
                    ORDER BY created_at, id
                    """,
                    (production_id, kind, expected_fingerprint),
                )
            ]
            for artifact_id in ids:
                try:
                    artifact = self._artifact_authority(db, artifact_id)
                    if kind in _DERIVATION_AUTHORITY:
                        self._validate_revision_derivation_producer(db, artifact)
                    return artifact
                except (InvalidArtifact, MissingRetainedArtifact, ArtifactDigestMismatch):
                    continue
        return None

    def _bound_sources(
        self,
        db: Any,
        source_entries: Any,
        *,
        production_id: str,
        expected_kinds: dict[str, str],
    ) -> dict[str, dict[str, Any]]:
        sources = super()._bound_sources(
            db,
            source_entries,
            production_id=production_id,
            expected_kinds=expected_kinds,
        )
        for artifact in sources.values():
            self._validate_revision_derivation_producer(db, artifact)
        return sources

    def _validate_composition_job(
        self,
        db: Any,
        job: dict[str, Any],
        payload: bytes,
        *,
        production_id: str,
        production_revision_id: str,
        variant_id: str,
        artifact_input_fingerprint: Any,
    ) -> None:
        variant, revision_payload = self._variant_context(
            db,
            production_id=production_id,
            production_revision_id=production_revision_id,
            variant_id=variant_id,
        )
        self._validate_variant_against_revision(variant, revision_payload)
        super()._validate_composition_job(
            db,
            job,
            payload,
            production_id=production_id,
            production_revision_id=production_revision_id,
            variant_id=variant_id,
            artifact_input_fingerprint=artifact_input_fingerprint,
        )

    def _validate_render_source_authority(
        self,
        db: Any,
        prepared: PreparedArtifact,
        *,
        production_id: str,
        production_revision_id: str | None,
        variant_id: str | None,
        producer_job_id: str | None,
    ) -> None:
        super()._validate_render_source_authority(
            db,
            prepared,
            production_id=production_id,
            production_revision_id=production_revision_id,
            variant_id=variant_id,
            producer_job_id=producer_job_id,
        )
        if producer_job_id is None:
            raise InvalidArtifact("rendered_video is missing producer JobSpec authority")
        job = self._job_authority(db, producer_job_id)
        source_entries = job["spec"].get("source_artifacts")
        if not isinstance(source_entries, list) or len(source_entries) != 1:
            raise InvalidArtifact("media.render source composition authority is invalid")
        source_id = source_entries[0].get("artifact_id") if isinstance(source_entries[0], dict) else None
        if not isinstance(source_id, str) or not source_id:
            raise InvalidArtifact("media.render source composition authority is invalid")
        composition_artifact = self._artifact_authority(db, source_id)
        composition_payload = self.objects.read_bytes(
            str(composition_artifact["object_digest"]),
            int(composition_artifact["byte_size"]),
            str(composition_artifact["object_relpath"]),
        )
        try:
            composition = json.loads(composition_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidArtifact("media.render source composition is unreadable") from exc
        canvas = composition.get("canvas") if isinstance(composition, dict) else None
        expected_fps = canvas.get("fps") if isinstance(canvas, dict) else None
        if type(expected_fps) is not int or expected_fps < 1:
            raise InvalidArtifact("media.render source composition frame rate is invalid")

        rendered_payload = self.objects.read_bytes(
            prepared.object_record.digest_sha256,
            prepared.object_record.byte_size,
            prepared.object_record.object_relpath,
        )
        authoritative_frame_rate = _probe_video_frame_rate(rendered_payload)
        recorded_frame_rate = (prepared.metadata or {}).get("frame_rate_result")
        if recorded_frame_rate != authoritative_frame_rate:
            raise InvalidArtifact("rendered_video frame-rate result is not authoritative for retained bytes")
        numerator = authoritative_frame_rate["numerator"]
        denominator = authoritative_frame_rate["denominator"]
        if numerator != expected_fps * denominator:
            raise InvalidArtifact("rendered_video frame rate does not match bound composition canvas")

    def _validate_cover_registration(
        self,
        db: Any,
        prepared: PreparedArtifact,
        *,
        production_id: str,
        production_revision_id: str | None,
        variant_id: str | None,
        producer_job_id: str | None,
    ) -> None:
        super()._validate_cover_registration(
            db,
            prepared,
            production_id=production_id,
            production_revision_id=production_revision_id,
            variant_id=variant_id,
            producer_job_id=producer_job_id,
        )
        if producer_job_id is None:
            raise InvalidArtifact("cover_image is missing producer JobSpec authority")
        job = self._job_authority(db, producer_job_id)
        source_entries = job["spec"].get("source_artifacts")
        if not isinstance(source_entries, list) or len(source_entries) != 1:
            raise InvalidArtifact("cover source authority is invalid")
        source_id = source_entries[0].get("artifact_id") if isinstance(source_entries[0], dict) else None
        if not isinstance(source_id, str) or not source_id:
            raise InvalidArtifact("cover source authority is invalid")
        self._validate_revision_derivation_producer(db, self._artifact_authority(db, source_id))

    @staticmethod
    def _validate_composition_payload(
        composition: Any,
        *,
        variant: dict[str, Any],
        composition_preferences: dict[str, Any],
        source_digests: dict[str, str],
    ) -> None:
        VariantArtifactAuthorityService._validate_composition_payload(
            composition,
            variant=variant,
            composition_preferences=composition_preferences,
            source_digests=source_digests,
        )
        intent = variant["intent"]
        if composition["duration_ms"] != intent["duration_min_ms"]:
            raise InvalidArtifact(
                "composition payload duration does not match deterministic compiler selection"
            )

    def _validate_revision_derivation_producer(
        self,
        db: Any,
        artifact: dict[str, Any],
    ) -> None:
        kind = str(artifact.get("kind"))
        authority = _DERIVATION_AUTHORITY.get(kind)
        if authority is None:
            raise InvalidArtifact("composition source kind has no M0 derivation producer authority")
        job_id = artifact.get("producer_job_id")
        attempt_id = artifact.get("producer_attempt_id")
        revision_id = artifact.get("production_revision_id")
        artifact_id = artifact.get("id")
        if (
            not isinstance(job_id, str)
            or not job_id
            or not isinstance(attempt_id, str)
            or not attempt_id
            or not isinstance(revision_id, str)
            or not revision_id
            or not isinstance(artifact_id, str)
            or not artifact_id
        ):
            raise InvalidArtifact("derivation source has no authoritative producer JobSpec/attempt")
        try:
            job = self._job_authority(db, job_id)
        except NotFound as exc:
            raise InvalidArtifact("derivation source producer JobSpec is missing") from exc
        if (
            str(job["production_id"]) != str(artifact["production_id"])
            or job["production_revision_id"] != revision_id
            or job["variant_id"] is not None
            or job["job_class"] != "ARTIFACT"
            or job["job_type"] != authority["job_type"]
            or job["semantic_capability"] != authority["capability"]
            or job["state"] != "SUCCEEDED"
        ):
            raise InvalidArtifact("derivation source producer JobSpec does not hold authoritative revision scope")

        attempt = db.execute(
            "SELECT job_id,state,executor_identity,result_json FROM attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
        if (
            not attempt
            or str(attempt["job_id"]) != job_id
            or attempt["state"] != "SUCCEEDED"
            or attempt["executor_identity"] != "deterministic-artifact-provider"
            or not attempt["result_json"]
        ):
            raise InvalidArtifact("derivation source producer Attempt is not authoritative and successful")
        try:
            result = json.loads(str(attempt["result_json"]))
        except json.JSONDecodeError as exc:
            raise InvalidArtifact("derivation source producer Attempt result is unreadable") from exc
        if not isinstance(result, dict) or result.get("artifact_ids") != [artifact_id]:
            raise InvalidArtifact("derivation source is not the successful producer Attempt output")
        if artifact.get("producer_stage") != "deterministic_artifact_provider":
            raise InvalidArtifact("derivation source producer stage is not authoritative")

        spec = job["spec"]
        if spec.get("schema_version") != 1:
            raise InvalidArtifact("derivation source JobSpec schema is invalid")
        execution_mode = spec.get("execution_mode")
        if not isinstance(execution_mode, str):
            raise InvalidArtifact("derivation source JobSpec execution mode is invalid")
        expected_route = self._qualified_route(str(authority["capability"]), spec)
        if job["route"] != expected_route:
            raise InvalidArtifact("derivation source producer route is not qualified")

        output_role = spec.get("output_role")
        if kind == "visual_image":
            if not isinstance(output_role, str) or not output_role.startswith("visual.") or len(output_role) <= 7:
                raise InvalidArtifact("visual derivation source output role is invalid")
            expected_filename = f"{output_role.replace('.', '-')}.png"
        else:
            if output_role != authority["output_role"]:
                raise InvalidArtifact("derivation source output role does not match producer capability")
            expected_filename = str(authority["filename"])
        self._validate_output_contract(
            spec,
            role=str(output_role),
            kind=kind,
            media_type=str(authority["media_type"]),
            filename=expected_filename,
        )

        revision = db.execute(
            "SELECT production_id,canonical_json FROM production_revisions WHERE id=?",
            (revision_id,),
        ).fetchone()
        if not revision or str(revision["production_id"]) != str(artifact["production_id"]):
            raise InvalidArtifact("derivation source producer revision authority is invalid")
        try:
            revision_payload = json.loads(str(revision["canonical_json"]))
        except json.JSONDecodeError as exc:
            raise InvalidArtifact("derivation source producer revision is unreadable") from exc
        if not isinstance(revision_payload, dict):
            raise InvalidArtifact("derivation source producer revision must be an object")

        semantic_inputs, source_object_digests = self._derivation_projection_for_producer(
            db,
            revision_payload,
            spec,
            production_id=str(artifact["production_id"]),
            revision_id=revision_id,
            capability=str(authority["capability"]),
            output_role=str(output_role),
        )
        if spec.get("semantic_inputs") != semantic_inputs:
            raise InvalidArtifact("derivation source producer semantics do not match its captured revision")
        if spec.get("source_object_digests") != source_object_digests:
            raise InvalidArtifact("derivation source producer Object digests do not match its captured revision")
        expected_fingerprint = self._expected_fingerprint(
            str(authority["capability"]),
            output_role=str(output_role),
            semantic_inputs=semantic_inputs,
            source_object_digests=source_object_digests,
            route=expected_route,
        )
        if (
            str(job["input_fingerprint"]) != expected_fingerprint
            or artifact.get("input_fingerprint") != expected_fingerprint
        ):
            raise InvalidArtifact("derivation source fingerprint does not match authoritative producer context")

    def _derivation_projection_for_producer(
        self,
        db: Any,
        revision_payload: dict[str, Any],
        spec: dict[str, Any],
        *,
        production_id: str,
        revision_id: str,
        capability: str,
        output_role: str,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        captured_defaults = revision_payload.get("captured_defaults")
        if not isinstance(captured_defaults, dict):
            raise InvalidArtifact("derivation producer revision is missing definition identity snapshots")

        if capability in {"speech.synthesize", "captions.generate"}:
            script = revision_payload.get("script")
            script_text = script.get("text") if isinstance(script, dict) else None
            if not isinstance(script_text, str) or not script_text:
                raise InvalidArtifact("derivation producer revision script is invalid")
            script_digest = hashlib.sha256(script_text.encode("utf-8")).hexdigest()
            self._validate_capture_source_entry(
                db,
                spec.get("source_artifacts"),
                role="script_text",
                kind="script_text",
                expected_digest=script_digest,
                production_id=production_id,
                revision_id=revision_id,
            )
            source_object_digests = {"script_text": script_digest}
            if capability == "speech.synthesize":
                voice = revision_payload.get("voice")
                if not isinstance(voice, dict):
                    raise InvalidArtifact("derivation producer voice preferences are invalid")
                pace = voice.get("pace")
                if not isinstance(pace, str) or not pace:
                    raise InvalidArtifact("derivation producer voice pace is invalid")
                semantic_inputs = {
                    "voice_profile": _captured_definition_identity(
                        captured_defaults,
                        "voice_profile",
                        "voice profile",
                        expected_reference=voice.get("voice_profile_ref"),
                    ),
                    "pace": pace,
                }
            else:
                captions = revision_payload.get("captions")
                if not isinstance(captions, dict) or captions.get("enabled", True) is not True:
                    raise InvalidArtifact("derivation producer caption preferences are invalid")
                brief = revision_payload.get("brief")
                language = brief.get("primary_language") if isinstance(brief, dict) else None
                if not isinstance(language, str) or not language:
                    raise InvalidArtifact("derivation producer caption language is invalid")
                semantic_inputs = {
                    "caption_style": _captured_definition_identity(
                        captured_defaults,
                        "caption_style",
                        "caption style",
                        expected_reference=captions.get("style_preset_ref"),
                    ),
                    "language": language,
                }
            return semantic_inputs, source_object_digests

        if capability == "image.generate":
            visual_plan = revision_payload.get("visual_plan")
            if not isinstance(visual_plan, list):
                raise InvalidArtifact("derivation producer visual plan is invalid")
            item_id = output_role.removeprefix("visual.")
            matches = [
                item
                for item in visual_plan
                if isinstance(item, dict) and item.get("item_id") == item_id
            ]
            if len(matches) != 1:
                raise InvalidArtifact("visual derivation source does not match one captured visual-plan item")
            visual_plan_digest = hashlib.sha256(canonical_text(visual_plan).encode("utf-8")).hexdigest()
            self._validate_capture_source_entry(
                db,
                spec.get("source_artifacts"),
                role="visual_plan",
                kind="visual_plan",
                expected_digest=visual_plan_digest,
                production_id=production_id,
                revision_id=revision_id,
            )
            return {
                "visual_plan_item_hash": canonical_hash(matches[0]),
                "visual_style": _captured_definition_identity(
                    captured_defaults,
                    "visual_style",
                    "visual style",
                    expected_reference="default",
                ),
            }, {}

        raise InvalidArtifact("unsupported M0 derivation producer capability")

    def _validate_capture_source_entry(
        self,
        db: Any,
        source_entries: Any,
        *,
        role: str,
        kind: str,
        expected_digest: str,
        production_id: str,
        revision_id: str,
    ) -> None:
        if (
            not isinstance(source_entries, list)
            or len(source_entries) != 1
            or not isinstance(source_entries[0], dict)
            or source_entries[0].get("role") != role
            or not isinstance(source_entries[0].get("artifact_id"), str)
            or not source_entries[0]["artifact_id"]
        ):
            raise InvalidArtifact("derivation producer source Artifact binding is invalid")
        try:
            source = self._artifact_authority(db, str(source_entries[0]["artifact_id"]))
        except NotFound as exc:
            raise InvalidArtifact("derivation producer captured source Artifact is missing") from exc
        if (
            str(source["production_id"]) != production_id
            or source["production_revision_id"] != revision_id
            or source["variant_id"] is not None
            or source["kind"] != kind
            or source["object_digest"] != expected_digest
            or source["producer_stage"] != "revision_capture"
            or source["producer_job_id"] is not None
            or source["producer_attempt_id"] is not None
        ):
            raise InvalidArtifact("derivation producer source is not authoritative captured revision input")

    @staticmethod
    def _validate_variant_against_revision(
        variant: dict[str, Any],
        revision_payload: dict[str, Any],
    ) -> None:
        intent = variant.get("intent")
        if not isinstance(intent, dict):
            raise InvalidArtifact("variant intent must be an object")

        brief = revision_payload.get("brief")
        if not isinstance(brief, dict):
            raise InvalidArtifact("captured revision brief must be an object")
        language = brief.get("primary_language")
        if not isinstance(language, str) or not language:
            raise InvalidArtifact("captured revision primary language is invalid")
        if intent.get("language") != language:
            raise InvalidArtifact(
                "M0 variant language adaptation requires a translated revision and is not implicit"
            )

        captions = revision_payload.get("captions")
        if not isinstance(captions, dict) or type(captions.get("enabled", True)) is not bool:
            raise InvalidArtifact("captured caption preferences are invalid")
        if intent.get("caption_mode") == "burned" and captions.get("enabled", True) is not True:
            raise InvalidArtifact("burned-caption variant requires captured captions to be enabled")

        visual_plan = revision_payload.get("visual_plan")
        if not isinstance(visual_plan, list):
            raise InvalidArtifact("captured visual plan must be a list")


def _probe_video_frame_rate(payload: bytes) -> dict[str, int | str]:
    with tempfile.TemporaryDirectory() as temp_dir:
        media_path = Path(temp_dir) / "render.mp4"
        media_path.write_bytes(payload)
        try:
            completed = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=avg_frame_rate",
                    "-of",
                    "json",
                    str(media_path),
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InvalidArtifact("media frame-rate validation could not run") from exc
    if completed.returncode != 0:
        raise InvalidArtifact("rendered media frame rate could not be probed")
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise InvalidArtifact("media frame-rate probe result is invalid") from exc
    streams = data.get("streams")
    if not isinstance(streams, list) or len(streams) != 1 or not isinstance(streams[0], dict):
        raise InvalidArtifact("media frame-rate probe result is incomplete")
    raw = streams[0].get("avg_frame_rate")
    if not isinstance(raw, str) or "/" not in raw:
        raise InvalidArtifact("media frame-rate probe value is invalid")
    numerator_text, denominator_text = raw.split("/", 1)
    try:
        numerator = int(numerator_text)
        denominator = int(denominator_text)
    except ValueError as exc:
        raise InvalidArtifact("media frame-rate probe value is invalid") from exc
    if numerator <= 0 or denominator <= 0:
        raise InvalidArtifact("media frame-rate probe value is invalid")
    divisor = math.gcd(numerator, denominator)
    return {
        "validator": "ffprobe-frame-rate-v1",
        "numerator": numerator // divisor,
        "denominator": denominator // divisor,
    }
