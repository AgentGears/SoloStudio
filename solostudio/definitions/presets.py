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
    key = "default" if ref is None else ref
    return _identity("voice profile", key, _VOICE_PROFILES)


def caption_style_identity(ref: str | None) -> dict[str, str]:
    key = "default" if ref is None else ref
    return _identity("caption style", key, _CAPTION_STYLES)


def visual_style_identity(ref: str | None = "default") -> dict[str, str]:
    key = "default" if ref is None else ref
    return _identity("visual style", key, _VISUAL_STYLES)


def _identity(label: str, key: str, definitions: dict[str, dict[str, Any]]) -> dict[str, str]:
    definition = definitions.get(key)
    if definition is None:
        raise InvalidCommand(f"unknown M0 {label} definition: {key}")
    return {
        "definition_id": str(definition["definition_id"]),
        "content_hash": canonical_hash(definition),
    }
