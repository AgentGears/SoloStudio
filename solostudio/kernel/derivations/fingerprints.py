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


FINGERPRINT_PROJECTIONS: dict[str, Callable[..., str]] = {
    "speech.synthesize": speech_synthesize,
    "captions.generate": captions_generate,
    "image.generate": image_generate,
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
