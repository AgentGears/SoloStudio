from __future__ import annotations

from typing import Any, Callable

from solostudio.kernel.errors import InvalidCommand
from solostudio.kernel.identity import canonical_hash


def _material_route(route: dict[str, Any]) -> dict[str, str]:
    material: dict[str, str] = {}
    for key in ("route_id", "provider", "model", "tool_profile"):
        value = route.get(key)
        if not isinstance(value, str) or not value:
            raise InvalidCommand(f"qualified route is missing material identity field: {key}")
        material[key] = value
    return material


def _build(
    capability: str,
    *,
    output_role: str,
    semantic_inputs: dict[str, Any],
    source_object_digests: dict[str, str],
    route: dict[str, Any],
) -> str:
    return canonical_hash(
        {
            "capability": capability,
            "output_role": output_role,
            "semantic_inputs": semantic_inputs,
            "source_object_digests": source_object_digests,
            "material_route": _material_route(route),
        }
    )


def speech_synthesize(
    *,
    output_role: str,
    semantic_inputs: dict[str, Any],
    source_object_digests: dict[str, str],
    route: dict[str, Any],
) -> str:
    if set(source_object_digests) != {"script_text"}:
        raise InvalidCommand("speech fingerprint requires exactly the script_text Object digest")
    if "voice_profile" not in semantic_inputs or "pace" not in semantic_inputs:
        raise InvalidCommand("speech fingerprint requires voice profile identity and pace")
    return _build(
        "speech.synthesize",
        output_role=output_role,
        semantic_inputs=semantic_inputs,
        source_object_digests=source_object_digests,
        route=route,
    )


def captions_generate(
    *,
    output_role: str,
    semantic_inputs: dict[str, Any],
    source_object_digests: dict[str, str],
    route: dict[str, Any],
) -> str:
    if set(source_object_digests) != {"script_text"}:
        raise InvalidCommand("caption fingerprint requires exactly the script_text Object digest")
    if "caption_style" not in semantic_inputs or "language" not in semantic_inputs:
        raise InvalidCommand("caption fingerprint requires caption style and language")
    return _build(
        "captions.generate",
        output_role=output_role,
        semantic_inputs=semantic_inputs,
        source_object_digests=source_object_digests,
        route=route,
    )


def image_generate(
    *,
    output_role: str,
    semantic_inputs: dict[str, Any],
    source_object_digests: dict[str, str],
    route: dict[str, Any],
) -> str:
    if source_object_digests:
        raise InvalidCommand("image fingerprint uses item semantic identity, not the whole visual-plan Object digest")
    if "visual_plan_item_hash" not in semantic_inputs or "visual_style" not in semantic_inputs:
        raise InvalidCommand("image fingerprint requires visual-plan item hash and visual style")
    return _build(
        "image.generate",
        output_role=output_role,
        semantic_inputs=semantic_inputs,
        source_object_digests=source_object_digests,
        route=route,
    )


def composition_compile(
    *,
    output_role: str,
    semantic_inputs: dict[str, Any],
    source_object_digests: dict[str, str],
    route: dict[str, Any],
) -> str:
    if output_role != "composition.primary":
        raise InvalidCommand("composition fingerprint requires the composition.primary output role")
    if set(semantic_inputs) != {"variant_intent_hash", "composition_preferences"}:
        raise InvalidCommand("composition fingerprint requires variant intent hash and composition preferences")
    if not _is_sha256(semantic_inputs.get("variant_intent_hash")):
        raise InvalidCommand("composition fingerprint variant intent hash is invalid")
    if not isinstance(semantic_inputs.get("composition_preferences"), dict):
        raise InvalidCommand("composition preferences must be an object")
    if "voice" not in source_object_digests:
        raise InvalidCommand("composition fingerprint requires voice Object digest")
    _validate_digest_map(source_object_digests)
    visual_keys = sorted(key for key in source_object_digests if key.startswith("visual."))
    expected_visual_keys = [f"visual.{index:04d}" for index in range(len(visual_keys))]
    if visual_keys != expected_visual_keys:
        raise InvalidCommand("composition visual Object digests must be contiguous and ordered")
    allowed = {"voice", "captions", *visual_keys}
    if set(source_object_digests) != allowed:
        raise InvalidCommand("composition fingerprint contains unsupported source Object roles")
    return _build(
        "composition.compile",
        output_role=output_role,
        semantic_inputs=semantic_inputs,
        source_object_digests=source_object_digests,
        route=route,
    )


def media_render(
    *,
    output_role: str,
    semantic_inputs: dict[str, Any],
    source_object_digests: dict[str, str],
    route: dict[str, Any],
) -> str:
    if output_role != "render.primary":
        raise InvalidCommand("media render fingerprint requires the render.primary output role")
    if semantic_inputs:
        raise InvalidCommand("media render fingerprint has no semantic inputs beyond the composition and route")
    if set(source_object_digests) != {"composition_spec"}:
        raise InvalidCommand("media render fingerprint requires exactly the composition_spec Object digest")
    _validate_digest_map(source_object_digests)
    return _build(
        "media.render",
        output_role=output_role,
        semantic_inputs=semantic_inputs,
        source_object_digests=source_object_digests,
        route=route,
    )


def cover_produce(
    *,
    output_role: str,
    semantic_inputs: dict[str, Any],
    source_object_digests: dict[str, str],
    route: dict[str, Any],
) -> str:
    if output_role != "cover.primary":
        raise InvalidCommand("cover fingerprint requires the cover.primary output role")
    if set(semantic_inputs) != {"cover_preferences"} or not isinstance(semantic_inputs["cover_preferences"], dict):
        raise InvalidCommand("cover fingerprint requires cover preferences")
    if set(source_object_digests) != {"selected_visual"}:
        raise InvalidCommand("cover fingerprint requires exactly the selected visual Object digest")
    _validate_digest_map(source_object_digests)
    return _build(
        "cover.produce",
        output_role=output_role,
        semantic_inputs=semantic_inputs,
        source_object_digests=source_object_digests,
        route=route,
    )


def _validate_digest_map(values: dict[str, str]) -> None:
    for role, digest in values.items():
        if not _is_sha256(digest):
            raise InvalidCommand(f"invalid Object digest for fingerprint role: {role}")


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


FINGERPRINT_PROJECTIONS: dict[str, Callable[..., str]] = {
    "speech.synthesize": speech_synthesize,
    "captions.generate": captions_generate,
    "image.generate": image_generate,
    "composition.compile": composition_compile,
    "media.render": media_render,
    "cover.produce": cover_produce,
}


def expected_fingerprint(
    capability: str,
    *,
    output_role: str,
    semantic_inputs: dict[str, Any],
    source_object_digests: dict[str, str],
    route: dict[str, Any],
) -> str:
    builder = FINGERPRINT_PROJECTIONS.get(capability)
    if builder is None:
        raise InvalidCommand(f"no M0 Artifact fingerprint projection for capability: {capability}")
    return builder(
        output_role=output_role,
        semantic_inputs=semantic_inputs,
        source_object_digests=source_object_digests,
        route=route,
    )
