from __future__ import annotations

from typing import Any

from solostudio.kernel.capabilities.router import CapabilityRouter
from solostudio.kernel.costs import CostPlan
from solostudio.kernel.errors import BudgetExceeded, CapabilityUnavailable, InvalidCommand
from solostudio.kernel.identity import canonical_hash
from solostudio.kernel.jobs import JobAdmission, JobService
from solostudio.kernel.productions import ProductionService


_JOB_TYPES = {
    "text.generate": "SCRIPT_GENERATE",
    "visual.plan": "VISUAL_PLAN_GENERATE",
}


class CapabilityService:
    def __init__(self, productions: ProductionService, jobs: JobService, router: CapabilityRouter | None = None) -> None:
        self.productions = productions
        self.jobs = jobs
        self.router = router or CapabilityRouter()

    def request_state_job(
        self,
        *,
        production_id: str,
        capability: str,
        execution_mode: str = "PRIVATE",
        max_cost_microunits: int = 0,
        max_attempts: int = 2,
    ) -> JobAdmission:
        if capability not in _JOB_TYPES:
            return self._unsupported(capability)
        if type(max_cost_microunits) is not int or max_cost_microunits < 0:
            raise InvalidCommand("max cost must be a non-negative integer microunit value")

        state = self.productions.working_state(production_id)
        projection = self._projection(capability, state["state"])
        route = self.router.qualify(capability, execution_mode=execution_mode)
        estimate = route.get("estimated_cost_microunits")
        if type(estimate) is not int or estimate < 0:
            raise InvalidCommand("qualified route must provide a non-negative integer cost estimate")
        if estimate > max_cost_microunits:
            raise BudgetExceeded(f"estimated cost {estimate} exceeds request limit {max_cost_microunits}")

        fingerprint = canonical_hash({
            "capability": capability,
            "source_state_version": state["state_version"],
            "projection": projection,
            "route_identity": {
                "route_id": route["route_id"],
                "provider": route["provider"],
                "model": route["model"],
                "tool_profile": route["tool_profile"],
            },
        })
        spec = {
            "schema_version": 1,
            "projection": projection,
            "execution_mode": execution_mode,
        }
        return self.jobs.admit(
            production_id=production_id,
            job_class="STATE_PROPOSAL",
            job_type=_JOB_TYPES[capability],
            semantic_capability=capability,
            spec=spec,
            route=route,
            input_fingerprint=fingerprint,
            max_attempts=max_attempts,
            cost_plan=CostPlan(capability, estimate, estimate),
            expected_source_state_version=int(state["state_version"]),
        )

    def execute_state_job(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.job(job_id)
        if job["job_class"] != "STATE_PROPOSAL":
            raise InvalidCommand("deterministic capability executor requires a state proposal job")
        capability = str(job["semantic_capability"])
        if capability not in _JOB_TYPES:
            return self._unsupported(capability)
        if job["state"] != "QUEUED":
            raise InvalidCommand("state proposal job must be queued before execution")
        execution_mode = job["spec"].get("execution_mode")
        if not isinstance(execution_mode, str):
            raise InvalidCommand("state proposal job is missing execution mode")
        expected_route = self.router.qualify(capability, execution_mode=execution_mode)
        if job["route"] != expected_route:
            raise InvalidCommand("persisted route does not match the qualified deterministic executor route")
        projection = job["spec"].get("projection")
        if not isinstance(projection, dict):
            raise InvalidCommand("state proposal job is missing projection")
        try:
            proposal = self._proposal(capability, projection)
        except (KeyError, TypeError, AttributeError) as exc:
            raise InvalidCommand("state proposal projection is invalid") from exc
        attempts = self.jobs.attempts(job_id)
        if not attempts or attempts[-1]["state"] != "CREATED":
            raise InvalidCommand("queued job has no created attempt")
        attempt_id = str(attempts[-1]["id"])
        self.jobs.start_attempt(attempt_id, "deterministic-state-provider")
        self.jobs.complete_state_proposal(attempt_id, proposal)
        return {"job_id": job_id, "attempt_id": attempt_id, "proposal": proposal}

    @staticmethod
    def _projection(capability: str, state: dict[str, Any]) -> dict[str, Any]:
        if capability == "text.generate":
            brief = state["brief"]
            return {
                "brief": {
                    key: brief[key]
                    for key in (
                        "topic", "audience", "goal", "tone",
                        "duration_min_ms", "duration_max_ms", "primary_language",
                    )
                }
            }
        if capability == "visual.plan":
            script = state["script"]
            if not script["text"]:
                raise InvalidCommand("visual planning requires script text")
            return {
                "script": {"text": script["text"], "status": script["status"]},
                "tone": state["brief"]["tone"],
            }
        raise AssertionError(capability)

    @staticmethod
    def _proposal(capability: str, projection: dict[str, Any]) -> dict[str, Any]:
        if capability == "text.generate":
            brief = projection["brief"]
            topic = brief["topic"].strip() or "the selected topic"
            audience = brief["audience"].strip() or "the intended audience"
            goal = brief["goal"].strip() or "explain the key idea clearly"
            tone = brief["tone"].strip() or "clear"
            language = brief["primary_language"].strip() or "en"
            text = (
                f"{topic}. For {audience}, {goal}. "
                f"Use a {tone} tone and keep the explanation concise. "
                f"Primary language: {language}."
            )
            return {"action": "set_script", "input": {"text": text, "status": "ready"}}
        if capability == "visual.plan":
            script = projection["script"]["text"].strip()
            tone = projection.get("tone") or "clear"
            excerpt = script[:180]
            items = [
                {"item_id": "scene-01", "purpose": "hook", "prompt": f"Opening visual for: {excerpt}"},
                {"item_id": "scene-02", "purpose": "explain", "prompt": f"Explanatory visual, {tone} tone: {excerpt}"},
                {"item_id": "scene-03", "purpose": "close", "prompt": f"Closing visual reinforcing: {excerpt}"},
            ]
            return {"action": "set_visual_plan", "input": {"items": items}}
        raise AssertionError(capability)

    @staticmethod
    def _unsupported(capability: str):
        raise CapabilityUnavailable(f"capability is not exposed by the M0 state provider: {capability}")
