from __future__ import annotations

from copy import deepcopy
from typing import Any

from solostudio.definitions import (
    caption_style_identity,
    visual_style_identity,
    voice_profile_identity,
)
from solostudio.kernel.errors import InvalidCommand


PRODUCTION_TYPE = "technical_explainer_v1"
WORKING_SCHEMA_VERSION = 1


DEFAULT_WORKING_STATE: dict[str, Any] = {
    "schema_version": 1,
    "brief": {
        "topic": "",
        "audience": "",
        "goal": "",
        "tone": "",
        "duration_min_ms": 45000,
        "duration_max_ms": 60000,
        "primary_language": "en",
    },
    "script": {"text": "", "status": "draft"},
    "voice": {"voice_profile_ref": None, "pace": "normal"},
    "captions": {"enabled": True, "style_preset_ref": "default"},
    "visual_plan": [],
    "composition_preferences": {},
    "selected_variant_ids": [],
    "notes": "",
}


BRIEF_FIELDS = frozenset(DEFAULT_WORKING_STATE["brief"])
VOICE_FIELDS = frozenset(DEFAULT_WORKING_STATE["voice"])
CAPTION_FIELDS = frozenset(DEFAULT_WORKING_STATE["captions"])


def new_working_state() -> dict[str, Any]:
    return deepcopy(DEFAULT_WORKING_STATE)


def reduce_state(state: dict[str, Any], action: str, command_input: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    new_state = deepcopy(state)

    if action in {"create_brief", "update_brief"}:
        patch = _dict_input(command_input, "brief")
        unknown = set(patch) - BRIEF_FIELDS
        if unknown:
            raise InvalidCommand(f"unsupported brief fields: {sorted(unknown)}")
        new_state["brief"].update(patch)
        _validate_brief(new_state["brief"])
        return new_state, [f"brief.{key}" for key in sorted(patch)]

    if action == "set_script":
        text = command_input.get("text")
        status = command_input.get("status", "draft")
        if not isinstance(text, str):
            raise InvalidCommand("script text must be a string")
        if status not in {"draft", "ready"}:
            raise InvalidCommand("script status must be draft or ready")
        new_state["script"] = {"text": text, "status": status}
        return new_state, ["script"]

    if action == "set_voice_preferences":
        patch = _dict_input(command_input, "voice")
        unknown = set(patch) - VOICE_FIELDS
        if unknown:
            raise InvalidCommand(f"unsupported voice fields: {sorted(unknown)}")
        new_state["voice"].update(patch)
        return new_state, [f"voice.{key}" for key in sorted(patch)]

    if action == "set_caption_preferences":
        patch = _dict_input(command_input, "captions")
        unknown = set(patch) - CAPTION_FIELDS
        if unknown:
            raise InvalidCommand(f"unsupported caption fields: {sorted(unknown)}")
        new_state["captions"].update(patch)
        return new_state, [f"captions.{key}" for key in sorted(patch)]

    if action == "set_visual_plan":
        plan = command_input.get("items")
        if not isinstance(plan, list):
            raise InvalidCommand("visual plan items must be a list")
        seen: set[str] = set()
        for item in plan:
            if not isinstance(item, dict) or not isinstance(item.get("item_id"), str):
                raise InvalidCommand("every visual plan item requires a string item_id")
            if item["item_id"] in seen:
                raise InvalidCommand("visual plan item_id values must be unique")
            seen.add(item["item_id"])
        new_state["visual_plan"] = deepcopy(plan)
        return new_state, ["visual_plan"]

    if action == "set_composition_preferences":
        value = command_input.get("preferences")
        if not isinstance(value, dict):
            raise InvalidCommand("composition preferences must be an object")
        new_state["composition_preferences"] = deepcopy(value)
        return new_state, ["composition_preferences"]

    if action == "select_variant":
        variant_id = command_input.get("variant_id")
        if not isinstance(variant_id, str) or not variant_id:
            raise InvalidCommand("variant_id must be a non-empty string")
        if variant_id not in new_state["selected_variant_ids"]:
            new_state["selected_variant_ids"].append(variant_id)
        return new_state, ["selected_variant_ids"]

    raise InvalidCommand(f"unsupported action: {action}")


def revision_payload(state: dict[str, Any], production_type: str = PRODUCTION_TYPE) -> dict[str, Any]:
    return {
        "schema_version": WORKING_SCHEMA_VERSION,
        "canonicalization_version": 1,
        "production_type": production_type,
        "brief": deepcopy(state["brief"]),
        "script": deepcopy(state["script"]),
        "voice": deepcopy(state["voice"]),
        "captions": deepcopy(state["captions"]),
        "visual_plan": deepcopy(state["visual_plan"]),
        "composition_preferences": deepcopy(state["composition_preferences"]),
        "captured_defaults": _captured_defaults(state),
    }


def _captured_defaults(state: dict[str, Any]) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    script = state.get("script", {}).get("text", "") if isinstance(state.get("script"), dict) else ""
    if isinstance(script, str) and script:
        voice = state.get("voice")
        if isinstance(voice, dict):
            captured["voice_profile"] = _definition_snapshot(
                voice.get("voice_profile_ref"),
                voice_profile_identity,
            )
        captions = state.get("captions")
        if isinstance(captions, dict) and captions.get("enabled", True) is True:
            captured["caption_style"] = _definition_snapshot(
                captions.get("style_preset_ref"),
                caption_style_identity,
            )

    visual_plan = state.get("visual_plan")
    if isinstance(visual_plan, list) and visual_plan:
        captured["visual_style"] = _definition_snapshot("default", visual_style_identity)

    return captured


def _definition_snapshot(reference: Any, resolver: Any) -> dict[str, Any]:
    stored_reference = "default" if reference is None else deepcopy(reference)
    try:
        identity = resolver(reference)
    except InvalidCommand:
        return {
            "reference": stored_reference,
            "definition_id": None,
            "content_hash": None,
            "resolved": False,
        }
    return {
        "reference": stored_reference,
        "definition_id": identity["definition_id"],
        "content_hash": identity["content_hash"],
        "resolved": True,
    }


def _dict_input(command_input: dict[str, Any], name: str) -> dict[str, Any]:
    value = command_input.get(name, command_input)
    if not isinstance(value, dict):
        raise InvalidCommand(f"{name} input must be an object")
    return value


def _validate_brief(brief: dict[str, Any]) -> None:
    if type(brief["duration_min_ms"]) is not int or type(brief["duration_max_ms"]) is not int:
        raise InvalidCommand("duration values must be integer milliseconds")
    if brief["duration_min_ms"] < 0 or brief["duration_max_ms"] < brief["duration_min_ms"]:
        raise InvalidCommand("duration range is invalid")
    for key in ("topic", "audience", "goal", "tone", "primary_language"):
        if not isinstance(brief[key], str):
            raise InvalidCommand(f"brief.{key} must be a string")
