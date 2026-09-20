from __future__ import annotations

from typing import Any

from solostudio.kernel.errors import InvalidCommand
from solostudio.kernel.identity import canonical_hash


_VOICE_PROFILES: dict[str, dict[str, Any]] = {
    "default": {
        "definition_id": "voice.default.v1",
        "engine_profile": "deterministic-tone-v1",
        "sample_rate_hz": 8000,
        "channels": 1,
    },
}

_CAPTION_STYLES: dict[str, dict[str, Any]] = {
    "default": {
        "definition_id": "captions.default.v1",
        "format": "webvtt",
        "placement": "bottom",
        "line_policy": "single-cue-m0",
    },
}

_VISUAL_STYLES: dict[str, dict[str, Any]] = {
    "default": {
        "definition_id": "visual.default.v1",
        "renderer_profile": "deterministic-png-v1",
        "width": 2,
        "height": 2,
    },
}


def voice_profile_identity(ref: str | None) -> dict[str, str]:
    return _identity("voice profile", _definition_key("voice profile", ref), _VOICE_PROFILES)


def caption_style_identity(ref: str | None) -> dict[str, str]:
    return _identity("caption style", _definition_key("caption style", ref), _CAPTION_STYLES)


def visual_style_identity(ref: str | None = "default") -> dict[str, str]:
    return _identity("visual style", _definition_key("visual style", ref), _VISUAL_STYLES)


def _definition_key(label: str, ref: str | None) -> str:
    if ref is None:
        return "default"
    if not isinstance(ref, str) or not ref:
        raise InvalidCommand(f"M0 {label} reference must be a non-empty string or null")
    return ref


def _identity(label: str, key: str, definitions: dict[str, dict[str, Any]]) -> dict[str, str]:
    definition = definitions.get(key)
    if definition is None:
        raise InvalidCommand(f"unknown M0 {label} definition: {key}")
    return {
        "definition_id": str(definition["definition_id"]),
        "content_hash": canonical_hash(definition),
    }
