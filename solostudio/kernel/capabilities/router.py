from __future__ import annotations

from copy import deepcopy
from typing import Any

from solostudio.kernel.errors import CapabilityUnavailable, InvalidCommand


_EXECUTION_MODES = {"PRIVATE", "BALANCED"}
_ROUTES: dict[str, dict[str, Any]] = {
    "text.generate": {
        "route_id": "route_text_generate_v1",
        "provider": "builtin_deterministic",
        "model": "script-template-v1",
        "tool_profile": "state-proposal-v1",
        "estimated_cost_microunits": 0,
        "billing_ambiguity_on_interrupt": False,
    },
    "visual.plan": {
        "route_id": "route_visual_plan_v1",
        "provider": "builtin_deterministic",
        "model": "visual-plan-template-v1",
        "tool_profile": "state-proposal-v1",
        "estimated_cost_microunits": 0,
        "billing_ambiguity_on_interrupt": False,
    },
    "speech.synthesize": {
        "route_id": "route_speech_synthesize_v1",
        "provider": "builtin_deterministic",
        "model": "deterministic-wave-v1",
        "tool_profile": "artifact-provider-v1",
        "estimated_cost_microunits": 0,
        "billing_ambiguity_on_interrupt": False,
    },
    "captions.generate": {
        "route_id": "route_captions_generate_v1",
        "provider": "builtin_deterministic",
        "model": "deterministic-vtt-v1",
        "tool_profile": "artifact-provider-v1",
        "estimated_cost_microunits": 0,
        "billing_ambiguity_on_interrupt": False,
    },
    "image.generate": {
        "route_id": "route_image_generate_v1",
        "provider": "builtin_deterministic",
        "model": "deterministic-png-v1",
        "tool_profile": "artifact-provider-v1",
        "estimated_cost_microunits": 0,
        "billing_ambiguity_on_interrupt": False,
    },
    "composition.compile": {
        "route_id": "route_composition_compile_v1",
        "provider": "builtin_deterministic",
        "model": "composition-spec-v1",
        "tool_profile": "artifact-provider-v1",
        "estimated_cost_microunits": 0,
        "billing_ambiguity_on_interrupt": False,
    },
    "cover.produce": {
        "route_id": "route_cover_produce_v1",
        "provider": "builtin_deterministic",
        "model": "cover-copy-v1",
        "tool_profile": "artifact-provider-v1",
        "estimated_cost_microunits": 0,
        "billing_ambiguity_on_interrupt": False,
    },
    "media.render": {
        "route_id": "route_media_render_v1",
        "provider": "local_ffmpeg",
        "model": "synthetic-media-v1",
        "tool_profile": "media-render-v1",
        "estimated_cost_microunits": 0,
        "billing_ambiguity_on_interrupt": False,
    },
}


class CapabilityRouter:
    def qualify(self, capability: str, *, execution_mode: str) -> dict[str, Any]:
        if execution_mode not in _EXECUTION_MODES:
            raise InvalidCommand(f"unsupported execution mode: {execution_mode}")
        route = _ROUTES.get(capability)
        if route is None:
            raise CapabilityUnavailable(f"no qualified M0 route for capability: {capability}")
        qualified = deepcopy(route)
        qualified["qualification_evidence"] = {
            "locality": "LOCAL",
            "execution_mode": execution_mode,
        }
        return qualified
