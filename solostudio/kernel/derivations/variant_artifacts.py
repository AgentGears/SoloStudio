from __future__ import annotations

import hashlib
import json
import struct
from typing import Any, Iterable

from solostudio.kernel.artifacts import PreparedArtifact
from solostudio.kernel.capabilities.router import CapabilityRouter
from solostudio.kernel.derivations.artifacts import DerivationArtifactService
from solostudio.kernel.derivations.fingerprints import expected_fingerprint
from solostudio.kernel.errors import InvalidArtifact, InvalidCommand, NotFound
from solostudio.kernel.identity import canonical_hash


_RENDER_DURATION_TOLERANCE_MS = 50
_MP4_BRANDS = {
    b"isom",
    b"iso2",
    b"iso3",
    b"iso4",
    b"iso5",
    b"iso6",
    b"mp41",
    b"mp42",
    b"avc1",
    b"M4V ",
    b"F4V ",
}


class VariantArtifactAuthorityService(DerivationArtifactService):
    """Adds Slice 8 composition/render authority checks at Artifact registration."""

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
        if kind == "rendered_video" and media_type == "video/mp4":
            _validate_mp4_brand(payload)
        return super().prepare_bytes(
            payload,
            kind=kind,
            media_type=media_type,
            producer_stage=producer_stage,
            input_fingerprint=input_fingerprint,
            metadata=metadata,
        )

    @staticmethod
    def _validate(kind: str, media_type: str, payload: bytes) -> None:
        DerivationArtifactService._validate(kind, media_type, payload)
        if kind == "rendered_video" and media_type == "video/mp4":
            _validate_mp4_brand(payload)

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
        if prepared.kind == "composition_spec":
            self._validate_composition_registration(
                db,
                prepared,
                production_id=production_id,
                production_revision_id=production_revision_id,
                variant_id=variant_id,
                producer_job_id=producer_job_id,
            )
        elif prepared.kind == "rendered_video":
            self._validate_render_source_authority(
                db,
                prepared,
                production_id=production_id,
                production_revision_id=production_revision_id,
                variant_id=variant_id,
                producer_job_id=producer_job_id,
            )

        return super().register_prepared_in_tx(
            db,
            prepared,
            production_id=production_id,
            production_revision_id=production_revision_id,
            variant_id=variant_id,
            producer_job_id=producer_job_id,
            producer_attempt_id=producer_attempt_id,
            dependencies=dependencies,
        )

    def _validate_composition_registration(
        self,
        db: Any,
        prepared: PreparedArtifact,
        *,
        production_id: str,
        production_revision_id: str | None,
        variant_id: str | None,
        producer_job_id: str | None,
    ) -> None:
        if production_revision_id is None or variant_id is None or producer_job_id is None:
            raise InvalidArtifact("composition_spec requires revision, variant, and producer JobSpec lineage")
        job = self._job_authority(db, producer_job_id)
        payload = self.objects.read_bytes(
            prepared.object_record.digest_sha256,
            prepared.object_record.byte_size,
            prepared.object_record.object_relpath,
        )
        self._validate_composition_job(
            db,
            job,
            payload,
            production_id=production_id,
            production_revision_id=production_revision_id,
            variant_id=variant_id,
            artifact_input_fingerprint=prepared.input_fingerprint,
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
        if production_revision_id is None or variant_id is None or producer_job_id is None:
            raise InvalidArtifact("rendered_video requires revision, variant, and producer JobSpec lineage")
        job = self._job_authority(db, producer_job_id)
        if (
            str(job["production_id"]) != production_id
            or job["production_revision_id"] != production_revision_id
            or job["variant_id"] != variant_id
            or job["job_class"] != "ARTIFACT"
            or job["job_type"] != "MEDIA_RENDER"
            or job["semantic_capability"] != "media.render"
        ):
            raise InvalidArtifact("rendered_video producer job does not hold media-render variant authority")
        spec = job["spec"]
        self._validate_output_contract(
            spec,
            role="render.primary",
            kind="rendered_video",
            media_type="video/mp4",
            filename="render.mp4",
        )
        if spec.get("semantic_inputs") != {}:
            raise InvalidArtifact("media.render semantic inputs must be empty")

        source_entries = spec.get("source_artifacts")
        if (
            not isinstance(source_entries, list)
            or len(source_entries) != 1
            or not isinstance(source_entries[0], dict)
            or source_entries[0].get("role") != "composition_spec"
            or not isinstance(source_entries[0].get("artifact_id"), str)
            or not source_entries[0]["artifact_id"]
        ):
            raise InvalidArtifact("media.render requires exactly one bound composition_spec Artifact")
        source = self._artifact_authority(db, str(source_entries[0]["artifact_id"]))
        if (
            str(source["production_id"]) != production_id
            or source["production_revision_id"] != production_revision_id
            or source["variant_id"] != variant_id
            or source["kind"] != "composition_spec"
        ):
            raise InvalidArtifact("media.render source is not the bound variant composition")

        source_digests = {"composition_spec": str(source["object_digest"])}
        if spec.get("source_object_digests") != source_digests:
            raise InvalidArtifact("media.render source digest does not match the bound composition Artifact")
        expected = self._expected_fingerprint(
            "media.render",
            output_role="render.primary",
            semantic_inputs={},
            source_object_digests=source_digests,
            route=job["route"],
        )
        if str(job["input_fingerprint"]) != expected or prepared.input_fingerprint != expected:
            raise InvalidArtifact("media.render fingerprint does not match the bound composition source")

        composition_job_id = source.get("producer_job_id")
        if not isinstance(composition_job_id, str) or not composition_job_id:
            raise InvalidArtifact("media.render source composition has no authoritative producer JobSpec")
        composition_job = self._job_authority(db, composition_job_id)
        composition_payload = self.objects.read_bytes(
            str(source["object_digest"]),
            int(source["byte_size"]),
            str(source["object_relpath"]),
        )
        self._validate("composition_spec", str(source["media_type"]), composition_payload)
        self._validate_composition_job(
            db,
            composition_job,
            composition_payload,
            production_id=production_id,
            production_revision_id=production_revision_id,
            variant_id=variant_id,
            artifact_input_fingerprint=source.get("input_fingerprint"),
        )

        try:
            composition = json.loads(composition_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidArtifact("media.render source composition is unreadable") from exc
        expected_duration = composition.get("duration_ms") if isinstance(composition, dict) else None
        if type(expected_duration) is not int:
            raise InvalidArtifact("media.render source composition duration is invalid")

        rendered_payload = self.objects.read_bytes(
            prepared.object_record.digest_sha256,
            prepared.object_record.byte_size,
            prepared.object_record.object_relpath,
        )
        _validate_mp4_brand(rendered_payload)
        recorded_probe = (prepared.metadata or {}).get("validator_result")
        if not isinstance(recorded_probe, dict):
            raise InvalidArtifact("rendered_video validator result is missing")
        rendered_duration = recorded_probe.get("duration_ms")
        if type(rendered_duration) is not int:
            raise InvalidArtifact("rendered_video validator duration is invalid")
        if abs(rendered_duration - expected_duration) > _RENDER_DURATION_TOLERANCE_MS:
            raise InvalidArtifact(
                "rendered_video duration does not match the bound composition within muxing tolerance"
            )

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
        if (
            str(job["production_id"]) != production_id
            or job["production_revision_id"] != production_revision_id
            or job["variant_id"] != variant_id
            or job["job_class"] != "ARTIFACT"
            or job["job_type"] != "COMPOSITION_COMPILE"
            or job["semantic_capability"] != "composition.compile"
        ):
            raise InvalidArtifact("composition_spec producer job does not hold composition authority")
        spec = job["spec"]
        self._validate_output_contract(
            spec,
            role="composition.primary",
            kind="composition_spec",
            media_type="application/json",
            filename="composition.json",
        )

        variant, revision_payload = self._variant_context(
            db,
            production_id=production_id,
            production_revision_id=production_revision_id,
            variant_id=variant_id,
        )
        composition_preferences = revision_payload.get("composition_preferences")
        if not isinstance(composition_preferences, dict):
            raise InvalidArtifact("captured composition preferences must be an object")
        semantic_inputs = {
            "variant_intent_hash": str(variant["intent_hash"]),
            "composition_preferences": composition_preferences,
        }
        if spec.get("semantic_inputs") != semantic_inputs:
            raise InvalidArtifact("composition semantic inputs do not match variant/revision authority")

        intent = variant["intent"]
        visual_plan = revision_payload.get("visual_plan")
        if not isinstance(visual_plan, list):
            raise InvalidArtifact("captured visual plan must be a list")
        expected_kinds: dict[str, str] = {"voice": "voice_audio"}
        if intent.get("caption_mode") == "burned":
            expected_kinds["captions"] = "caption_track"
        seen_items: set[str] = set()
        for index, item in enumerate(visual_plan):
            if not isinstance(item, dict) or not isinstance(item.get("item_id"), str) or not item["item_id"]:
                raise InvalidArtifact("captured visual plan item identity is invalid")
            item_id = str(item["item_id"])
            if item_id in seen_items:
                raise InvalidArtifact("captured visual plan item identity is duplicated")
            seen_items.add(item_id)
            expected_kinds[f"visual.{index:04d}"] = "visual_image"

        sources = self._bound_sources(
            db,
            spec.get("source_artifacts"),
            production_id=production_id,
            expected_kinds=expected_kinds,
        )
        execution_mode = spec.get("execution_mode")
        if not isinstance(execution_mode, str):
            raise InvalidArtifact("composition JobSpec execution mode is invalid")
        expected_sources = _expected_derivation_requirements(
            revision_payload,
            intent,
            visual_plan,
            execution_mode=execution_mode,
        )
        if set(expected_sources) != set(sources):
            raise InvalidArtifact("composition source roles do not match the captured revision requirements")
        for role, artifact in sources.items():
            expected_kind, expected_fingerprint_value = expected_sources[role]
            if str(artifact["kind"]) != expected_kind:
                raise InvalidArtifact("composition source Artifact kind does not match current derivation requirement")
            if artifact.get("input_fingerprint") != expected_fingerprint_value:
                raise InvalidArtifact("composition source is not current for the bound revision context")

        source_digests = {role: str(sources[role]["object_digest"]) for role in expected_kinds}
        if spec.get("source_object_digests") != source_digests:
            raise InvalidArtifact("composition source Object digests do not match bound source Artifacts")
        expected = self._expected_fingerprint(
            "composition.compile",
            output_role="composition.primary",
            semantic_inputs=semantic_inputs,
            source_object_digests=source_digests,
            route=job["route"],
        )
        if str(job["input_fingerprint"]) != expected or artifact_input_fingerprint != expected:
            raise InvalidArtifact("composition fingerprint does not match authoritative bound sources")

        self._validate("composition_spec", "application/json", payload)
        try:
            composition = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidArtifact("composition_spec payload is unreadable") from exc
        self._validate_composition_payload(
            composition,
            variant=variant,
            composition_preferences=composition_preferences,
            source_digests=source_digests,
        )

    def _bound_sources(
        self,
        db: Any,
        source_entries: Any,
        *,
        production_id: str,
        expected_kinds: dict[str, str],
    ) -> dict[str, dict[str, Any]]:
        if not isinstance(source_entries, list) or len(source_entries) != len(expected_kinds):
            raise InvalidArtifact("composition source_artifacts do not match required roles")
        result: dict[str, dict[str, Any]] = {}
        for entry in source_entries:
            if not isinstance(entry, dict):
                raise InvalidArtifact("composition source_artifacts entries must be objects")
            role = entry.get("role")
            artifact_id = entry.get("artifact_id")
            if role not in expected_kinds or not isinstance(artifact_id, str) or not artifact_id:
                raise InvalidArtifact("composition source Artifact role or id is invalid")
            if role in result:
                raise InvalidArtifact("composition source Artifact role is duplicated")
            artifact = self._artifact_authority(db, artifact_id)
            if str(artifact["production_id"]) != production_id:
                raise InvalidArtifact("composition source Artifact belongs to another production")
            if artifact["variant_id"] is not None:
                raise InvalidArtifact("composition derivation source must not be variant-scoped")
            if artifact["kind"] != expected_kinds[role]:
                raise InvalidArtifact("composition source Artifact kind does not match its role")
            result[str(role)] = artifact
        if set(result) != set(expected_kinds):
            raise InvalidArtifact("composition source Artifact roles are incomplete")
        return result

    def _artifact_authority(self, db: Any, artifact_id: str) -> dict[str, Any]:
        row = db.execute(
            """
            SELECT a.*,o.byte_size,o.object_relpath
            FROM artifacts a JOIN objects o ON o.digest_sha256=a.object_digest
            WHERE a.id=?
            """,
            (artifact_id,),
        ).fetchone()
        if not row:
            raise NotFound(f"artifact not found: {artifact_id}")
        artifact = dict(row)
        self.objects.verify(
            str(artifact["object_digest"]),
            int(artifact["byte_size"]),
            str(artifact["object_relpath"]),
        )
        payload = self.objects.read_bytes(
            str(artifact["object_digest"]),
            int(artifact["byte_size"]),
            str(artifact["object_relpath"]),
        )
        self._validate(str(artifact["kind"]), str(artifact["media_type"]), payload)
        return artifact

    @staticmethod
    def _validate_output_contract(
        spec: dict[str, Any],
        *,
        role: str,
        kind: str,
        media_type: str,
        filename: str,
    ) -> None:
        if (
            spec.get("output_role") != role
            or spec.get("kind") != kind
            or spec.get("media_type") != media_type
            or spec.get("filename") != filename
        ):
            raise InvalidArtifact("Artifact JobSpec output contract does not match its capability")

    @staticmethod
    def _validate_composition_payload(
        composition: Any,
        *,
        variant: dict[str, Any],
        composition_preferences: dict[str, Any],
        source_digests: dict[str, str],
    ) -> None:
        if not isinstance(composition, dict):
            raise InvalidArtifact("composition_spec payload must be an object")
        intent = variant["intent"]
        if composition.get("variant_intent_hash") != variant["intent_hash"]:
            raise InvalidArtifact("composition payload intent does not match the bound variant")
        if composition.get("composition_preferences") != composition_preferences:
            raise InvalidArtifact("composition payload preferences do not match captured revision authority")
        if composition.get("canvas") != _canvas_for_intent(intent):
            raise InvalidArtifact("composition payload canvas does not match the bound variant")
        duration = composition.get("duration_ms")
        if type(duration) is not int or not (
            intent["duration_min_ms"] <= duration <= intent["duration_max_ms"]
        ):
            raise InvalidArtifact("composition payload duration is outside variant bounds")

        tracks = composition.get("tracks")
        if not isinstance(tracks, list):
            raise InvalidArtifact("composition payload tracks must be a list")
        expected_track_kinds = ["visual", "voice"]
        if intent.get("caption_mode") == "burned":
            expected_track_kinds.append("captions")
        track_kinds = [track.get("kind") if isinstance(track, dict) else None for track in tracks]
        if track_kinds != expected_track_kinds:
            raise InvalidArtifact("composition payload track set/order does not match the bound variant")

        visual_keys = sorted(key for key in source_digests if key.startswith("visual."))
        expected_visual_items = [{"object_digest": source_digests[key]} for key in visual_keys]
        if tracks[0].get("items") != expected_visual_items:
            raise InvalidArtifact("composition visual track digests do not match bound visual Artifacts")
        if tracks[1].get("object_digest") != source_digests["voice"]:
            raise InvalidArtifact("composition voice digest does not match the bound voice Artifact")
        if intent.get("caption_mode") == "burned":
            if tracks[2].get("object_digest") != source_digests["captions"]:
                raise InvalidArtifact("composition caption digest does not match the bound caption Artifact")
            if tracks[2].get("style") != {"mode": "burned"}:
                raise InvalidArtifact("composition caption treatment does not match the bound variant")

    @staticmethod
    def _job_authority(db: Any, job_id: str) -> dict[str, Any]:
        row = db.execute("SELECT * FROM job_specs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise NotFound(f"job not found: {job_id}")
        job = dict(row)
        try:
            spec = json.loads(str(job["spec_json"]))
            route = json.loads(str(job["route_json"]))
        except json.JSONDecodeError as exc:
            raise InvalidArtifact("Artifact JobSpec authority JSON is unreadable") from exc
        if not isinstance(spec, dict) or not isinstance(route, dict):
            raise InvalidArtifact("Artifact JobSpec authority JSON must be objects")
        job["spec"] = spec
        job["route"] = route
        return job

    @staticmethod
    def _variant_context(
        db: Any,
        *,
        production_id: str,
        production_revision_id: str,
        variant_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        variant_row = db.execute(
            "SELECT production_id,source_revision_id,intent_json,intent_hash FROM delivery_variants WHERE id=?",
            (variant_id,),
        ).fetchone()
        if not variant_row:
            raise NotFound(f"variant not found: {variant_id}")
        variant = dict(variant_row)
        if (
            str(variant["production_id"]) != production_id
            or str(variant["source_revision_id"]) != production_revision_id
        ):
            raise InvalidArtifact("variant lineage does not match Artifact JobSpec authority")
        revision = db.execute(
            "SELECT production_id,canonical_json FROM production_revisions WHERE id=?",
            (production_revision_id,),
        ).fetchone()
        if not revision or str(revision["production_id"]) != production_id:
            raise InvalidArtifact("captured revision lineage does not match Artifact JobSpec authority")
        try:
            intent = json.loads(str(variant["intent_json"]))
            revision_payload = json.loads(str(revision["canonical_json"]))
        except json.JSONDecodeError as exc:
            raise InvalidArtifact("variant or revision authority JSON is unreadable") from exc
        if not isinstance(intent, dict) or not isinstance(revision_payload, dict):
            raise InvalidArtifact("variant or revision authority payload must be an object")
        variant["intent"] = intent
        return variant, revision_payload

    @staticmethod
    def _expected_fingerprint(
        capability: str,
        *,
        output_role: str,
        semantic_inputs: dict[str, Any],
        source_object_digests: dict[str, str],
        route: dict[str, Any],
    ) -> str:
        try:
            return expected_fingerprint(
                capability,
                output_role=output_role,
                semantic_inputs=semantic_inputs,
                source_object_digests=source_object_digests,
                route=route,
            )
        except InvalidCommand as exc:
            raise InvalidArtifact("Artifact JobSpec fingerprint projection is invalid") from exc


def _expected_derivation_requirements(
    revision_payload: dict[str, Any],
    intent: dict[str, Any],
    visual_plan: list[Any],
    *,
    execution_mode: str,
) -> dict[str, tuple[str, str]]:
    script = revision_payload.get("script")
    if not isinstance(script, dict) or not isinstance(script.get("text"), str) or not script["text"]:
        raise InvalidArtifact("captured revision cannot satisfy the required voice derivation")
    script_text = str(script["text"])
    script_digest = hashlib.sha256(script_text.encode("utf-8")).hexdigest()

    captured_defaults = revision_payload.get("captured_defaults")
    if not isinstance(captured_defaults, dict):
        raise InvalidArtifact("captured revision is missing definition identity snapshots")

    voice = revision_payload.get("voice")
    if not isinstance(voice, dict):
        raise InvalidArtifact("captured voice preferences must be an object")
    pace = voice.get("pace")
    if not isinstance(pace, str) or not pace:
        raise InvalidArtifact("captured voice pace must be a non-empty string")
    voice_semantic = {
        "voice_profile": _captured_definition_identity(
            captured_defaults,
            "voice_profile",
            "voice profile",
            expected_reference=voice.get("voice_profile_ref"),
        ),
        "pace": pace,
    }
    router = CapabilityRouter()
    voice_route = router.qualify("speech.synthesize", execution_mode=execution_mode)
    result: dict[str, tuple[str, str]] = {
        "voice": (
            "voice_audio",
            _safe_expected_fingerprint(
                "speech.synthesize",
                output_role="voice.primary",
                semantic_inputs=voice_semantic,
                source_object_digests={"script_text": script_digest},
                route=voice_route,
            ),
        )
    }

    if intent.get("caption_mode") == "burned":
        captions = revision_payload.get("captions")
        if not isinstance(captions, dict):
            raise InvalidArtifact("captured caption preferences must be an object")
        enabled = captions.get("enabled", True)
        if enabled is not True:
            raise InvalidArtifact("burned-caption variant requires current captured captions")
        brief = revision_payload.get("brief")
        language = brief.get("primary_language") if isinstance(brief, dict) else None
        if not isinstance(language, str) or not language:
            raise InvalidArtifact("captured primary language must be a non-empty string")
        caption_semantic = {
            "caption_style": _captured_definition_identity(
                captured_defaults,
                "caption_style",
                "caption style",
                expected_reference=captions.get("style_preset_ref"),
            ),
            "language": language,
        }
        caption_route = router.qualify("captions.generate", execution_mode=execution_mode)
        result["captions"] = (
            "caption_track",
            _safe_expected_fingerprint(
                "captions.generate",
                output_role="captions.primary",
                semantic_inputs=caption_semantic,
                source_object_digests={"script_text": script_digest},
                route=caption_route,
            ),
        )

    if visual_plan:
        visual_style = _captured_definition_identity(
            captured_defaults,
            "visual_style",
            "visual style",
            expected_reference="default",
        )
        image_route = router.qualify("image.generate", execution_mode=execution_mode)
        seen: set[str] = set()
        for index, item in enumerate(visual_plan):
            if not isinstance(item, dict):
                raise InvalidArtifact("captured visual plan items must be objects")
            item_id = item.get("item_id")
            if not isinstance(item_id, str) or not item_id or item_id in seen:
                raise InvalidArtifact("captured visual plan item_id values must be unique non-empty strings")
            seen.add(item_id)
            result[f"visual.{index:04d}"] = (
                "visual_image",
                _safe_expected_fingerprint(
                    "image.generate",
                    output_role=f"visual.{item_id}",
                    semantic_inputs={
                        "visual_plan_item_hash": canonical_hash(item),
                        "visual_style": visual_style,
                    },
                    source_object_digests={},
                    route=image_route,
                ),
            )
    return result


def _captured_definition_identity(
    captured_defaults: dict[str, Any],
    key: str,
    label: str,
    *,
    expected_reference: Any,
) -> dict[str, str]:
    snapshot = captured_defaults.get(key)
    if not isinstance(snapshot, dict):
        raise InvalidArtifact(f"captured revision is missing {label} identity")
    normalized_reference = "default" if expected_reference is None else expected_reference
    if snapshot.get("reference") != normalized_reference:
        raise InvalidArtifact(f"captured {label} reference does not match revision preferences")
    if snapshot.get("resolved") is not True:
        raise InvalidArtifact(f"captured revision contains an unresolved {label} definition")
    definition_id = snapshot.get("definition_id")
    content_hash = snapshot.get("content_hash")
    if not isinstance(definition_id, str) or not definition_id:
        raise InvalidArtifact(f"captured {label} definition id is invalid")
    if (
        not isinstance(content_hash, str)
        or len(content_hash) != 64
        or any(char not in "0123456789abcdef" for char in content_hash)
    ):
        raise InvalidArtifact(f"captured {label} content hash is invalid")
    return {"definition_id": definition_id, "content_hash": content_hash}


def _safe_expected_fingerprint(
    capability: str,
    *,
    output_role: str,
    semantic_inputs: dict[str, Any],
    source_object_digests: dict[str, str],
    route: dict[str, Any],
) -> str:
    try:
        return expected_fingerprint(
            capability,
            output_role=output_role,
            semantic_inputs=semantic_inputs,
            source_object_digests=source_object_digests,
            route=route,
        )
    except InvalidCommand as exc:
        raise InvalidArtifact("captured derivation fingerprint projection is invalid") from exc


def _validate_mp4_brand(payload: bytes) -> None:
    if len(payload) < 16:
        raise InvalidArtifact("rendered media container is not MP4")
    offset = 0
    scan_limit = min(len(payload), 65_536)
    while offset + 8 <= scan_limit:
        box_size = struct.unpack(">I", payload[offset:offset + 4])[0]
        box_type = payload[offset + 4:offset + 8]
        header_size = 8
        if box_size == 1:
            if offset + 16 > len(payload):
                raise InvalidArtifact("rendered media container is not MP4")
            box_size = struct.unpack(">Q", payload[offset + 8:offset + 16])[0]
            header_size = 16
        elif box_size == 0:
            box_size = len(payload) - offset
        if box_size < header_size or offset + box_size > len(payload):
            raise InvalidArtifact("rendered media container is not MP4")
        if box_type == b"ftyp":
            body = payload[offset + header_size:offset + box_size]
            if len(body) < 8 or (len(body) - 8) % 4 != 0:
                raise InvalidArtifact("rendered media container is not MP4")
            major_brand = body[:4]
            compatible = {body[index:index + 4] for index in range(8, len(body), 4)}
            if major_brand == b"qt  ":
                raise InvalidArtifact("rendered media container is not MP4")
            if major_brand not in _MP4_BRANDS and not (compatible & _MP4_BRANDS):
                raise InvalidArtifact("rendered media container is not MP4")
            return
        offset += box_size
        if offset >= scan_limit:
            break
    raise InvalidArtifact("rendered media container is not MP4")


def _canvas_for_intent(intent: Any) -> dict[str, Any]:
    if not isinstance(intent, dict):
        raise InvalidArtifact("variant intent must be an object")
    aspect = intent.get("aspect_ratio")
    if aspect == "9:16":
        width, height = 1080, 1920
    elif aspect == "1:1":
        width, height = 1080, 1080
    else:
        raise InvalidArtifact("variant aspect ratio is unsupported")
    return {"aspect_ratio": aspect, "width": width, "height": height, "fps": 30}
