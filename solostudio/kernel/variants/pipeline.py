from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solostudio.kernel.costs import CostPlan
from solostudio.kernel.derivations import DerivationArtifactService, DerivationService
from solostudio.kernel.derivations.artifacts import probe_media_file
from solostudio.kernel.derivations.fingerprints import expected_fingerprint
from solostudio.kernel.derivations.models import PlannedArtifact
from solostudio.kernel.errors import (
    ArtifactDigestMismatch,
    BudgetExceeded,
    InvalidArtifact,
    InvalidCommand,
    MissingRetainedArtifact,
    SoloStudioError,
)
from solostudio.kernel.identity import canonical_text
from solostudio.kernel.jobs import JobService
from solostudio.kernel.productions import ProductionService
from solostudio.kernel.variants.service import VariantService


_JOB_TYPES = {
    "composition.compile": "COMPOSITION_COMPILE",
    "media.render": "MEDIA_RENDER",
    "cover.produce": "COVER_PRODUCE",
}


@dataclass(frozen=True, slots=True)
class _CompositionContext:
    production_id: str
    revision_id: str
    variant_id: str
    variant_intent_hash: str
    intent: dict[str, Any]
    composition_preferences: dict[str, Any]
    source_artifacts: tuple[tuple[str, str], ...]
    source_object_digests: dict[str, str]


class VariantPipelineService:
    """Variant-scoped composition/render plus the independent M0 cover path."""

    def __init__(
        self,
        productions: ProductionService,
        variants: VariantService,
        derivations: DerivationService,
        artifacts: DerivationArtifactService,
        jobs: JobService,
    ) -> None:
        self.productions = productions
        self.variants = variants
        self.derivations = derivations
        self.artifacts = artifacts
        self.jobs = jobs
        self.router = derivations.router

    def plan_inputs(
        self,
        variant_id: str,
        *,
        execution_mode: str = "PRIVATE",
        max_cost_microunits: int = 0,
        max_attempts: int = 2,
    ) -> list[PlannedArtifact]:
        self._validate_limits(max_cost_microunits, max_attempts)
        variant, payload = self._variant_payload(variant_id)
        self._validate_variant_against_revision(variant, payload)
        return self._plan_variant_inputs(
            variant,
            payload,
            execution_mode=execution_mode,
            max_cost_microunits=max_cost_microunits,
            max_attempts=max_attempts,
        )

    def plan_composition(
        self,
        variant_id: str,
        *,
        execution_mode: str = "PRIVATE",
        max_cost_microunits: int = 0,
        max_attempts: int = 2,
    ) -> PlannedArtifact:
        self._validate_limits(max_cost_microunits, max_attempts)
        context = self._materialized_composition_context(
            variant_id,
            execution_mode=execution_mode,
            max_cost_microunits=max_cost_microunits,
            max_attempts=max_attempts,
        )
        route = self.router.qualify("composition.compile", execution_mode=execution_mode)
        semantic_inputs = {
            "variant_intent_hash": context.variant_intent_hash,
            "composition_preferences": context.composition_preferences,
        }
        fingerprint = expected_fingerprint(
            "composition.compile",
            output_role="composition.primary",
            semantic_inputs=semantic_inputs,
            source_object_digests=context.source_object_digests,
            route=route,
        )
        reusable = self._find_variant_artifact(
            context.production_id,
            context.variant_id,
            "composition_spec",
            fingerprint,
        )
        if reusable is not None:
            return PlannedArtifact(
                "composition.primary",
                "composition.compile",
                "composition_spec",
                fingerprint,
                "REUSED_ARTIFACT",
                artifact_id=str(reusable["id"]),
            )

        estimate = self._estimate(route, max_cost_microunits)
        spec = {
            "schema_version": 1,
            "execution_mode": execution_mode,
            "output_role": "composition.primary",
            "kind": "composition_spec",
            "media_type": "application/json",
            "filename": "composition.json",
            "semantic_inputs": semantic_inputs,
            "source_object_digests": context.source_object_digests,
            "source_artifacts": [
                {"artifact_id": artifact_id, "role": role}
                for artifact_id, role in context.source_artifacts
            ],
        }
        admission = self.jobs.admit(
            production_id=context.production_id,
            production_revision_id=context.revision_id,
            variant_id=context.variant_id,
            job_class="ARTIFACT",
            job_type=_JOB_TYPES["composition.compile"],
            semantic_capability="composition.compile",
            spec=spec,
            route=route,
            input_fingerprint=fingerprint,
            max_attempts=max_attempts,
            cost_plan=CostPlan("composition.compile", estimate, estimate),
        )
        return PlannedArtifact(
            "composition.primary",
            "composition.compile",
            "composition_spec",
            fingerprint,
            "REUSED_JOB" if admission.reused else "ADMITTED_JOB",
            job_id=admission.job_id,
            attempt_id=admission.attempt_id,
        )

    def plan_render(
        self,
        variant_id: str,
        *,
        execution_mode: str = "PRIVATE",
        max_cost_microunits: int = 0,
        max_attempts: int = 2,
    ) -> PlannedArtifact:
        self._validate_limits(max_cost_microunits, max_attempts)
        context = self._materialized_composition_context(
            variant_id,
            execution_mode=execution_mode,
            max_cost_microunits=max_cost_microunits,
            max_attempts=max_attempts,
        )
        composition = self._current_composition(context, execution_mode)
        if composition is None:
            raise InvalidCommand("variant composition is not materialized; compile it before render planning")

        route = self.router.qualify("media.render", execution_mode=execution_mode)
        source_digests = {"composition_spec": str(composition["object_digest"])}
        fingerprint = expected_fingerprint(
            "media.render",
            output_role="render.primary",
            semantic_inputs={},
            source_object_digests=source_digests,
            route=route,
        )
        reusable = self._find_variant_artifact(
            context.production_id,
            context.variant_id,
            "rendered_video",
            fingerprint,
        )
        if reusable is not None:
            return PlannedArtifact(
                "render.primary",
                "media.render",
                "rendered_video",
                fingerprint,
                "REUSED_ARTIFACT",
                artifact_id=str(reusable["id"]),
            )

        estimate = self._estimate(route, max_cost_microunits)
        spec = {
            "schema_version": 1,
            "execution_mode": execution_mode,
            "output_role": "render.primary",
            "kind": "rendered_video",
            "media_type": "video/mp4",
            "filename": "render.mp4",
            "semantic_inputs": {},
            "source_object_digests": source_digests,
            "source_artifacts": [
                {"artifact_id": str(composition["id"]), "role": "composition_spec"}
            ],
        }
        admission = self.jobs.admit(
            production_id=context.production_id,
            production_revision_id=context.revision_id,
            variant_id=context.variant_id,
            job_class="ARTIFACT",
            job_type=_JOB_TYPES["media.render"],
            semantic_capability="media.render",
            spec=spec,
            route=route,
            input_fingerprint=fingerprint,
            max_attempts=max_attempts,
            cost_plan=CostPlan("media.render", estimate, estimate),
        )
        return PlannedArtifact(
            "render.primary",
            "media.render",
            "rendered_video",
            fingerprint,
            "REUSED_JOB" if admission.reused else "ADMITTED_JOB",
            job_id=admission.job_id,
            attempt_id=admission.attempt_id,
        )

    def plan_cover(
        self,
        selected_visual_artifact_id: str,
        *,
        cover_preferences: dict[str, Any] | None = None,
        execution_mode: str = "PRIVATE",
        max_cost_microunits: int = 0,
        max_attempts: int = 2,
    ) -> PlannedArtifact:
        self._validate_limits(max_cost_microunits, max_attempts)
        preferences = {} if cover_preferences is None else cover_preferences
        if not isinstance(preferences, dict):
            raise InvalidCommand("cover preferences must be an object")
        visual = self.artifacts.artifact(selected_visual_artifact_id, verify_bytes=True)
        if str(visual["kind"]) != "visual_image":
            raise InvalidCommand("cover source must be a visual_image Artifact")
        self.artifacts._validate(
            str(visual["kind"]),
            str(visual["media_type"]),
            self.artifacts.read_bytes(selected_visual_artifact_id),
        )
        revision_id = visual.get("production_revision_id")
        if not isinstance(revision_id, str) or not revision_id:
            raise InvalidCommand("cover source must retain captured revision provenance")
        production_id = str(visual["production_id"])
        route = self.router.qualify("cover.produce", execution_mode=execution_mode)
        source_digests = {"selected_visual": str(visual["object_digest"])}
        semantic_inputs = {"cover_preferences": preferences}
        fingerprint = expected_fingerprint(
            "cover.produce",
            output_role="cover.primary",
            semantic_inputs=semantic_inputs,
            source_object_digests=source_digests,
            route=route,
        )
        reusable = self.artifacts.find_reusable(production_id, "cover_image", fingerprint)
        if reusable is not None and reusable.get("variant_id") is None:
            return PlannedArtifact(
                "cover.primary",
                "cover.produce",
                "cover_image",
                fingerprint,
                "REUSED_ARTIFACT",
                artifact_id=str(reusable["id"]),
            )

        estimate = self._estimate(route, max_cost_microunits)
        spec = {
            "schema_version": 1,
            "execution_mode": execution_mode,
            "output_role": "cover.primary",
            "kind": "cover_image",
            "media_type": "image/png",
            "filename": "cover.png",
            "semantic_inputs": semantic_inputs,
            "source_object_digests": source_digests,
            "source_artifacts": [
                {"artifact_id": selected_visual_artifact_id, "role": "selected_visual"}
            ],
        }
        admission = self.jobs.admit(
            production_id=production_id,
            production_revision_id=revision_id,
            job_class="ARTIFACT",
            job_type=_JOB_TYPES["cover.produce"],
            semantic_capability="cover.produce",
            spec=spec,
            route=route,
            input_fingerprint=fingerprint,
            max_attempts=max_attempts,
            cost_plan=CostPlan("cover.produce", estimate, estimate),
        )
        return PlannedArtifact(
            "cover.primary",
            "cover.produce",
            "cover_image",
            fingerprint,
            "REUSED_JOB" if admission.reused else "ADMITTED_JOB",
            job_id=admission.job_id,
            attempt_id=admission.attempt_id,
        )

    def execute_job(self, job_id: str) -> str:
        job = self.jobs.job(job_id)
        capability = str(job["semantic_capability"])
        if capability == "composition.compile":
            return self._execute_composition(job)
        if capability == "media.render":
            return self._execute_render(job)
        if capability == "cover.produce":
            return self._execute_cover(job)
        raise InvalidCommand(f"unsupported variant pipeline capability: {capability}")

    def _execute_composition(self, job: dict[str, Any]) -> str:
        self._validate_job_header(job, "composition.compile")
        spec = job["spec"]
        execution_mode = self._execution_mode(spec)
        expected_route = self.router.qualify("composition.compile", execution_mode=execution_mode)
        if job["route"] != expected_route:
            raise InvalidCommand("persisted composition route does not match qualified route")
        variant_id = self._job_variant_id(job)
        variant, payload = self._variant_payload(variant_id)
        if str(variant["source_revision_id"]) != str(job["production_revision_id"]):
            raise InvalidCommand("composition job revision does not match variant lineage")
        self._validate_variant_against_revision(variant, payload)

        source_artifacts, source_digests = self._validate_composition_sources(
            job,
            variant,
            payload,
            execution_mode,
        )
        semantic_inputs = {
            "variant_intent_hash": str(variant["intent_hash"]),
            "composition_preferences": self._composition_preferences(payload),
        }
        if spec.get("semantic_inputs") != semantic_inputs:
            raise InvalidCommand("composition semantic inputs do not match variant/revision authority")
        if spec.get("source_object_digests") != source_digests:
            raise InvalidCommand("composition source Object digests do not match source Artifacts")
        fingerprint = expected_fingerprint(
            "composition.compile",
            output_role="composition.primary",
            semantic_inputs=semantic_inputs,
            source_object_digests=source_digests,
            route=expected_route,
        )
        if fingerprint != str(job["input_fingerprint"]):
            raise InvalidCommand("composition fingerprint does not match authoritative context")
        self._validate_output_contract(
            spec,
            "composition.primary",
            "composition_spec",
            "application/json",
            "composition.json",
        )

        composition = self._compile_composition(variant, source_artifacts)
        payload_bytes = canonical_text(composition).encode("utf-8")
        attempt_id = self._created_attempt(job)
        temp_dir = self.jobs.start_attempt(attempt_id, "deterministic-artifact-provider")
        try:
            (temp_dir / "composition.json").write_bytes(payload_bytes)
            artifact_ids = self.jobs.complete_artifact_attempt(
                attempt_id,
                [
                    {
                        "role": "composition.primary",
                        "path": "composition.json",
                        "kind": "composition_spec",
                        "media_type": "application/json",
                        "producer_stage": "variant_composition_compiler",
                    }
                ],
            )
        except (SoloStudioError, OSError) as exc:
            self.jobs.fail_attempt(
                attempt_id,
                getattr(exc, "code", "COMPOSITION_COMPILE_FAILED"),
                str(exc),
            )
            raise
        return self._single_artifact(artifact_ids)

    def _execute_render(self, job: dict[str, Any]) -> str:
        self._validate_job_header(job, "media.render")
        spec = job["spec"]
        execution_mode = self._execution_mode(spec)
        expected_route = self.router.qualify("media.render", execution_mode=execution_mode)
        if job["route"] != expected_route:
            raise InvalidCommand("persisted render route does not match qualified route")
        variant_id = self._job_variant_id(job)
        variant, _payload = self._variant_payload(variant_id)
        if str(variant["source_revision_id"]) != str(job["production_revision_id"]):
            raise InvalidCommand("render job revision does not match variant lineage")

        source = self._single_source(job, "composition_spec", "composition_spec")
        if source.get("variant_id") != variant_id:
            raise InvalidCommand("render source composition is not scoped to the bound variant")
        source_digest = str(source["object_digest"])
        source_digests = {"composition_spec": source_digest}
        if spec.get("semantic_inputs") != {} or spec.get("source_object_digests") != source_digests:
            raise InvalidCommand("render fingerprint projection does not match composition source")
        fingerprint = expected_fingerprint(
            "media.render",
            output_role="render.primary",
            semantic_inputs={},
            source_object_digests=source_digests,
            route=expected_route,
        )
        if fingerprint != str(job["input_fingerprint"]):
            raise InvalidCommand("render fingerprint does not match authoritative composition source")
        self._validate_output_contract(
            spec,
            "render.primary",
            "rendered_video",
            "video/mp4",
            "render.mp4",
        )

        composition_bytes = self.artifacts.read_bytes(str(source["id"]))
        self.artifacts._validate("composition_spec", "application/json", composition_bytes)
        composition = json.loads(composition_bytes.decode("utf-8"))
        self._validate_composition_for_variant(composition, variant)

        attempt_id = self._created_attempt(job)
        temp_dir = self.jobs.start_attempt(attempt_id, "deterministic-media-renderer")
        output_path = temp_dir / "render.mp4"
        try:
            self._render_fixture(output_path, composition, fingerprint)
            probe = probe_media_file(output_path)
            self._validate_probe(probe, composition, variant)
            artifact_ids = self.jobs.complete_artifact_attempt(
                attempt_id,
                [
                    {
                        "role": "render.primary",
                        "path": "render.mp4",
                        "kind": "rendered_video",
                        "media_type": "video/mp4",
                        "producer_stage": "local_media_renderer",
                    }
                ],
            )
        except (SoloStudioError, OSError) as exc:
            self.jobs.fail_attempt(
                attempt_id,
                getattr(exc, "code", "MEDIA_RENDER_FAILED"),
                str(exc),
            )
            raise
        return self._single_artifact(artifact_ids)

    def _execute_cover(self, job: dict[str, Any]) -> str:
        self._validate_job_header(job, "cover.produce", variant_required=False)
        spec = job["spec"]
        execution_mode = self._execution_mode(spec)
        expected_route = self.router.qualify("cover.produce", execution_mode=execution_mode)
        if job["route"] != expected_route:
            raise InvalidCommand("persisted cover route does not match qualified route")
        source = self._single_source(job, "selected_visual", "visual_image")
        preferences = spec.get("semantic_inputs")
        if (
            not isinstance(preferences, dict)
            or set(preferences) != {"cover_preferences"}
            or not isinstance(preferences["cover_preferences"], dict)
        ):
            raise InvalidCommand("cover semantic inputs are invalid")
        source_digests = {"selected_visual": str(source["object_digest"])}
        if spec.get("source_object_digests") != source_digests:
            raise InvalidCommand("cover source Object digest does not match selected visual")
        fingerprint = expected_fingerprint(
            "cover.produce",
            output_role="cover.primary",
            semantic_inputs=preferences,
            source_object_digests=source_digests,
            route=expected_route,
        )
        if fingerprint != str(job["input_fingerprint"]):
            raise InvalidCommand("cover fingerprint does not match selected visual context")
        self._validate_output_contract(
            spec,
            "cover.primary",
            "cover_image",
            "image/png",
            "cover.png",
        )

        payload = self.artifacts.read_bytes(str(source["id"]))
        self.artifacts._validate("visual_image", "image/png", payload)
        attempt_id = self._created_attempt(job)
        temp_dir = self.jobs.start_attempt(attempt_id, "deterministic-artifact-provider")
        try:
            (temp_dir / "cover.png").write_bytes(payload)
            artifact_ids = self.jobs.complete_artifact_attempt(
                attempt_id,
                [
                    {
                        "role": "cover.primary",
                        "path": "cover.png",
                        "kind": "cover_image",
                        "media_type": "image/png",
                        "producer_stage": "deterministic_cover_provider",
                    }
                ],
            )
        except (SoloStudioError, OSError) as exc:
            self.jobs.fail_attempt(
                attempt_id,
                getattr(exc, "code", "COVER_PRODUCE_FAILED"),
                str(exc),
            )
            raise
        return self._single_artifact(artifact_ids)

    def _plan_variant_inputs(
        self,
        variant: dict[str, Any],
        payload: dict[str, Any],
        *,
        execution_mode: str,
        max_cost_microunits: int,
        max_attempts: int,
    ) -> list[PlannedArtifact]:
        revision_id = str(variant["source_revision_id"])
        production_id = str(variant["production_id"])
        required_roles = set(self._required_output_roles(variant, payload))
        requirements = self.derivations._requirements(
            revision_id,
            payload,
            execution_mode=execution_mode,
        )
        requirements_by_role = {requirement.output_role: requirement for requirement in requirements}
        missing = sorted(required_roles - set(requirements_by_role))
        if missing:
            raise InvalidCommand(
                "captured revision cannot satisfy variant input roles: " + ", ".join(missing)
            )

        planned: list[PlannedArtifact] = []
        for requirement in requirements:
            if requirement.output_role not in required_roles:
                continue
            reusable = self.artifacts.find_reusable(
                production_id,
                requirement.kind,
                requirement.input_fingerprint,
            )
            if reusable is not None:
                planned.append(
                    PlannedArtifact(
                        requirement.output_role,
                        requirement.capability,
                        requirement.kind,
                        requirement.input_fingerprint,
                        "REUSED_ARTIFACT",
                        artifact_id=str(reusable["id"]),
                    )
                )
                continue

            estimate = self._estimate(requirement.route, max_cost_microunits)
            spec = {
                "schema_version": 1,
                "execution_mode": execution_mode,
                "output_role": requirement.output_role,
                "kind": requirement.kind,
                "media_type": requirement.media_type,
                "filename": requirement.filename,
                "semantic_inputs": requirement.semantic_inputs,
                "source_object_digests": requirement.source_object_digests,
                "source_artifacts": [
                    {"artifact_id": artifact_id, "role": role}
                    for artifact_id, role in requirement.source_artifacts
                ],
            }
            admission = self.jobs.admit(
                production_id=production_id,
                production_revision_id=revision_id,
                job_class="ARTIFACT",
                job_type=requirement.job_type,
                semantic_capability=requirement.capability,
                spec=spec,
                route=requirement.route,
                input_fingerprint=requirement.input_fingerprint,
                max_attempts=max_attempts,
                cost_plan=CostPlan(requirement.capability, estimate, estimate),
            )
            planned.append(
                PlannedArtifact(
                    requirement.output_role,
                    requirement.capability,
                    requirement.kind,
                    requirement.input_fingerprint,
                    "REUSED_JOB" if admission.reused else "ADMITTED_JOB",
                    job_id=admission.job_id,
                    attempt_id=admission.attempt_id,
                )
            )
        return planned

    def _materialized_composition_context(
        self,
        variant_id: str,
        *,
        execution_mode: str,
        max_cost_microunits: int,
        max_attempts: int,
    ) -> _CompositionContext:
        variant, payload = self._variant_payload(variant_id)
        self._validate_variant_against_revision(variant, payload)
        plans = self._plan_variant_inputs(
            variant,
            payload,
            execution_mode=execution_mode,
            max_cost_microunits=max_cost_microunits,
            max_attempts=max_attempts,
        )
        by_role = {plan.output_role: plan for plan in plans}
        required = self._required_output_roles(variant, payload)
        pending = [
            role
            for role in required
            if role not in by_role or by_role[role].disposition != "REUSED_ARTIFACT"
        ]
        if pending:
            raise InvalidCommand(
                "variant inputs are not materialized; execute planned ArtifactJobs first: "
                + ", ".join(pending)
            )

        source_artifacts: list[tuple[str, str]] = []
        source_digests: dict[str, str] = {}
        voice = self.artifacts.artifact(str(by_role["voice.primary"].artifact_id), verify_bytes=True)
        source_artifacts.append((str(voice["id"]), "voice"))
        source_digests["voice"] = str(voice["object_digest"])

        if str(variant["intent"]["caption_mode"]) == "burned":
            captions = self.artifacts.artifact(str(by_role["captions.primary"].artifact_id), verify_bytes=True)
            source_artifacts.append((str(captions["id"]), "captions"))
            source_digests["captions"] = str(captions["object_digest"])

        visual_plan = payload.get("visual_plan")
        if not isinstance(visual_plan, list):
            raise InvalidCommand("captured visual plan must be a list")
        for index, item in enumerate(visual_plan):
            if not isinstance(item, dict) or not isinstance(item.get("item_id"), str) or not item["item_id"]:
                raise InvalidCommand("captured visual plan item identity is invalid")
            role = f"visual.{item['item_id']}"
            visual = self.artifacts.artifact(str(by_role[role].artifact_id), verify_bytes=True)
            dependency_role = f"visual.{index:04d}"
            source_artifacts.append((str(visual["id"]), dependency_role))
            source_digests[dependency_role] = str(visual["object_digest"])

        return _CompositionContext(
            production_id=str(variant["production_id"]),
            revision_id=str(variant["source_revision_id"]),
            variant_id=variant_id,
            variant_intent_hash=str(variant["intent_hash"]),
            intent=dict(variant["intent"]),
            composition_preferences=self._composition_preferences(payload),
            source_artifacts=tuple(source_artifacts),
            source_object_digests=source_digests,
        )

    def _validate_composition_sources(
        self,
        job: dict[str, Any],
        variant: dict[str, Any],
        payload: dict[str, Any],
        execution_mode: str,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        spec_sources = job["spec"].get("source_artifacts")
        if not isinstance(spec_sources, list):
            raise InvalidCommand("composition source_artifacts must be a list")
        expected_requirements = {
            requirement.output_role: requirement
            for requirement in self.derivations._requirements(
                str(variant["source_revision_id"]),
                payload,
                execution_mode=execution_mode,
            )
        }
        required_output_roles = self._required_output_roles(variant, payload)
        expected_dependency_roles: dict[str, str] = {"voice": "voice.primary"}
        if str(variant["intent"]["caption_mode"]) == "burned":
            expected_dependency_roles["captions"] = "captions.primary"
        visual_plan = payload.get("visual_plan")
        if not isinstance(visual_plan, list):
            raise InvalidCommand("captured visual plan must be a list")
        for index, item in enumerate(visual_plan):
            if not isinstance(item, dict) or not isinstance(item.get("item_id"), str) or not item["item_id"]:
                raise InvalidCommand("captured visual plan item identity is invalid")
            expected_dependency_roles[f"visual.{index:04d}"] = f"visual.{item['item_id']}"
        if set(expected_dependency_roles.values()) != set(required_output_roles):
            raise InvalidCommand("composition source role projection is inconsistent")

        result: dict[str, dict[str, Any]] = {}
        digests: dict[str, str] = {}
        for source_entry in spec_sources:
            if not isinstance(source_entry, dict):
                raise InvalidCommand("composition source entry must be an object")
            artifact_id = source_entry.get("artifact_id")
            dependency_role = source_entry.get("role")
            if not isinstance(artifact_id, str) or dependency_role not in expected_dependency_roles:
                raise InvalidCommand("composition source entry has an invalid role or Artifact id")
            if dependency_role in result:
                raise InvalidCommand("composition source role is duplicated")
            artifact = self.artifacts.artifact(artifact_id, verify_bytes=True)
            if str(artifact["production_id"]) != str(job["production_id"]):
                raise InvalidCommand("composition source belongs to another production")
            expected_requirement = expected_requirements[expected_dependency_roles[dependency_role]]
            if artifact.get("input_fingerprint") != expected_requirement.input_fingerprint:
                raise InvalidCommand("composition source is not current for the bound revision context")
            if str(artifact["kind"]) != expected_requirement.kind:
                raise InvalidCommand("composition source Artifact kind does not match required role")
            self.artifacts._validate(
                str(artifact["kind"]),
                str(artifact["media_type"]),
                self.artifacts.read_bytes(artifact_id),
            )
            result[dependency_role] = artifact
            digests[dependency_role] = str(artifact["object_digest"])
        if set(result) != set(expected_dependency_roles):
            raise InvalidCommand("composition source roles are incomplete")
        return result, digests

    def _compile_composition(
        self,
        variant: dict[str, Any],
        sources: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        intent = variant["intent"]
        visual_roles = sorted(role for role in sources if role.startswith("visual."))
        tracks: list[dict[str, Any]] = [
            {
                "kind": "visual",
                "items": [
                    {"object_digest": str(sources[role]["object_digest"])}
                    for role in visual_roles
                ],
            },
            {"kind": "voice", "object_digest": str(sources["voice"]["object_digest"])},
        ]
        if intent["caption_mode"] == "burned":
            tracks.append(
                {
                    "kind": "captions",
                    "object_digest": str(sources["captions"]["object_digest"]),
                    "style": {"mode": "burned"},
                }
            )
        return {
            "schema_version": 1,
            "variant_intent_hash": str(variant["intent_hash"]),
            "canvas": self.variants.canvas(intent),
            "duration_ms": int(intent["duration_min_ms"]),
            "tracks": tracks,
        }

    def _current_composition(
        self,
        context: _CompositionContext,
        execution_mode: str,
    ) -> dict[str, Any] | None:
        route = self.router.qualify("composition.compile", execution_mode=execution_mode)
        fingerprint = expected_fingerprint(
            "composition.compile",
            output_role="composition.primary",
            semantic_inputs={
                "variant_intent_hash": context.variant_intent_hash,
                "composition_preferences": context.composition_preferences,
            },
            source_object_digests=context.source_object_digests,
            route=route,
        )
        return self._find_variant_artifact(
            context.production_id,
            context.variant_id,
            "composition_spec",
            fingerprint,
        )

    def _find_variant_artifact(
        self,
        production_id: str,
        variant_id: str,
        kind: str,
        fingerprint: str,
    ) -> dict[str, Any] | None:
        with self.artifacts.store.read() as db:
            ids = [
                str(row["id"])
                for row in db.execute(
                    """
                    SELECT id FROM artifacts
                    WHERE production_id=? AND variant_id=? AND kind=? AND input_fingerprint=?
                    ORDER BY created_at,id
                    """,
                    (production_id, variant_id, kind, fingerprint),
                )
            ]
        for artifact_id in ids:
            try:
                artifact = self.artifacts.artifact(artifact_id, verify_bytes=True)
                self.artifacts._validate(
                    str(artifact["kind"]),
                    str(artifact["media_type"]),
                    self.artifacts.read_bytes(artifact_id),
                )
                return artifact
            except (InvalidArtifact, MissingRetainedArtifact, ArtifactDigestMismatch):
                continue
        return None

    def _single_source(self, job: dict[str, Any], role: str, kind: str) -> dict[str, Any]:
        sources = job["spec"].get("source_artifacts")
        if not isinstance(sources, list) or len(sources) != 1 or not isinstance(sources[0], dict):
            raise InvalidCommand(f"{job['semantic_capability']} requires exactly one source Artifact")
        source_entry = sources[0]
        if source_entry.get("role") != role or not isinstance(source_entry.get("artifact_id"), str):
            raise InvalidCommand(f"{job['semantic_capability']} source role is invalid")
        artifact = self.artifacts.artifact(str(source_entry["artifact_id"]), verify_bytes=True)
        if str(artifact["production_id"]) != str(job["production_id"]):
            raise InvalidCommand("source Artifact belongs to another production")
        if str(artifact["kind"]) != kind:
            raise InvalidCommand(f"source Artifact must be {kind}")
        return artifact

    def _variant_payload(self, variant_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        variant = self.variants.variant(variant_id)
        revision = self.productions.revision(str(variant["source_revision_id"]))
        if str(revision["production_id"]) != str(variant["production_id"]):
            raise InvalidCommand("variant source revision and production lineage disagree")
        payload = json.loads(str(revision["canonical_json"]))
        if not isinstance(payload, dict):
            raise InvalidCommand("captured production revision payload must be an object")
        return variant, payload

    @staticmethod
    def _composition_preferences(payload: dict[str, Any]) -> dict[str, Any]:
        preferences = payload.get("composition_preferences")
        if not isinstance(preferences, dict):
            raise InvalidCommand("captured composition preferences must be an object")
        return preferences

    def _validate_variant_against_revision(self, variant: dict[str, Any], payload: dict[str, Any]) -> None:
        intent = variant["intent"]
        brief = payload.get("brief")
        if not isinstance(brief, dict):
            raise InvalidCommand("captured revision brief must be an object")
        language = brief.get("primary_language")
        if intent["language"] != language:
            raise InvalidCommand("M0 variant language adaptation requires a translated revision and is not implicit")
        captions = payload.get("captions")
        if not isinstance(captions, dict) or type(captions.get("enabled", True)) is not bool:
            raise InvalidCommand("captured caption preferences are invalid")
        if intent["caption_mode"] == "burned" and captions.get("enabled", True) is not True:
            raise InvalidCommand("burned-caption variant requires captured captions to be enabled")
        visual_plan = payload.get("visual_plan")
        if not isinstance(visual_plan, list):
            raise InvalidCommand("captured visual plan must be a list")

    def _required_output_roles(self, variant: dict[str, Any], payload: dict[str, Any]) -> list[str]:
        roles = ["voice.primary"]
        if variant["intent"]["caption_mode"] == "burned":
            roles.append("captions.primary")
        visual_plan = payload.get("visual_plan")
        if not isinstance(visual_plan, list):
            raise InvalidCommand("captured visual plan must be a list")
        for item in visual_plan:
            if not isinstance(item, dict) or not isinstance(item.get("item_id"), str) or not item["item_id"]:
                raise InvalidCommand("captured visual plan item identity is invalid")
            roles.append(f"visual.{item['item_id']}")
        return roles

    @staticmethod
    def _validate_job_header(job: dict[str, Any], capability: str, *, variant_required: bool = True) -> None:
        if job["job_class"] != "ARTIFACT" or str(job["job_type"]) != _JOB_TYPES[capability]:
            raise InvalidCommand("Artifact job class/type does not match variant pipeline capability")
        if job["state"] != "QUEUED":
            raise InvalidCommand("variant pipeline job must be queued before execution")
        if variant_required and (not isinstance(job.get("variant_id"), str) or not job["variant_id"]):
            raise InvalidCommand("variant-scoped Artifact job requires variant lineage")
        if not variant_required and job.get("variant_id") is not None:
            raise InvalidCommand("independent cover Artifact job must not be variant-scoped")

    @staticmethod
    def _job_variant_id(job: dict[str, Any]) -> str:
        variant_id = job.get("variant_id")
        if not isinstance(variant_id, str) or not variant_id:
            raise InvalidCommand("variant job is missing variant lineage")
        return variant_id

    @staticmethod
    def _execution_mode(spec: dict[str, Any]) -> str:
        value = spec.get("execution_mode")
        if not isinstance(value, str):
            raise InvalidCommand("Artifact job is missing execution mode")
        return value

    @staticmethod
    def _validate_output_contract(
        spec: dict[str, Any],
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
            raise InvalidCommand("Artifact job output contract does not match capability")

    def _created_attempt(self, job: dict[str, Any]) -> str:
        attempts = self.jobs.attempts(str(job["id"]))
        if not attempts or attempts[-1]["state"] != "CREATED":
            raise InvalidCommand("queued Artifact job has no created attempt")
        return str(attempts[-1]["id"])

    @staticmethod
    def _single_artifact(artifact_ids: list[str]) -> str:
        if len(artifact_ids) != 1:
            raise RuntimeError("single-output Artifact job registered an unexpected output count")
        return artifact_ids[0]

    @staticmethod
    def _estimate(route: dict[str, Any], max_cost_microunits: int) -> int:
        estimate = route.get("estimated_cost_microunits")
        if type(estimate) is not int or estimate < 0:
            raise InvalidCommand("qualified route must provide a non-negative integer cost estimate")
        if estimate > max_cost_microunits:
            raise BudgetExceeded(f"estimated cost {estimate} exceeds request limit {max_cost_microunits}")
        return estimate

    @staticmethod
    def _validate_limits(max_cost_microunits: int, max_attempts: int) -> None:
        if type(max_cost_microunits) is not int or max_cost_microunits < 0:
            raise InvalidCommand("max cost must be a non-negative integer microunit value")
        if type(max_attempts) is not int or max_attempts < 1:
            raise InvalidCommand("max_attempts must be a positive integer")

    def _validate_composition_for_variant(self, composition: dict[str, Any], variant: dict[str, Any]) -> None:
        intent = variant["intent"]
        if composition.get("variant_intent_hash") != variant["intent_hash"]:
            raise InvalidCommand("composition intent hash does not match variant")
        if composition.get("canvas") != self.variants.canvas(intent):
            raise InvalidCommand("composition canvas does not match variant intent")
        duration = composition.get("duration_ms")
        if type(duration) is not int or not (
            intent["duration_min_ms"] <= duration <= intent["duration_max_ms"]
        ):
            raise InvalidCommand("composition duration is outside variant bounds")
        tracks = composition.get("tracks")
        if not isinstance(tracks, list):
            raise InvalidCommand("composition tracks are invalid")
        kinds = [track.get("kind") for track in tracks if isinstance(track, dict)]
        if intent["caption_mode"] == "burned" and "captions" not in kinds:
            raise InvalidCommand("burned-caption variant composition is missing captions")
        if intent["caption_mode"] == "none" and "captions" in kinds:
            raise InvalidCommand("caption-free variant composition unexpectedly contains captions")
        if intent["audio_mode"] == "voiceover" and "voice" not in kinds:
            raise InvalidCommand("voiceover variant composition is missing voice")

    @staticmethod
    def _render_fixture(path: Path, composition: dict[str, Any], fingerprint: str) -> None:
        canvas = composition["canvas"]
        duration_ms = int(composition["duration_ms"])
        color = fingerprint[:6]
        frequency = 220 + (int(fingerprint[6:10], 16) % 440)
        duration_seconds = f"{duration_ms / 1000:.3f}"
        command = [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x{color}:s={int(canvas['width'])}x{int(canvas['height'])}:r={int(canvas['fps'])}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:sample_rate=48000",
            "-t",
            duration_seconds,
            "-map_metadata",
            "-1",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-movflags",
            "+faststart",
            str(path),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InvalidArtifact("local media renderer could not execute") from exc
        if completed.returncode != 0:
            message = completed.stderr.strip()[-1000:]
            raise InvalidArtifact(f"local media renderer failed: {message}")

    @staticmethod
    def _validate_probe(
        probe: dict[str, Any],
        composition: dict[str, Any],
        variant: dict[str, Any],
    ) -> None:
        canvas = composition["canvas"]
        intent = variant["intent"]
        if probe.get("width") != canvas["width"] or probe.get("height") != canvas["height"]:
            raise InvalidArtifact("rendered media dimensions do not match variant canvas")
        duration = probe.get("duration_ms")
        if type(duration) is not int or not (
            intent["duration_min_ms"] <= duration <= intent["duration_max_ms"]
        ):
            raise InvalidArtifact("rendered media duration is outside variant bounds")
        if duration > 90_000:
            raise InvalidArtifact("rendered media exceeds the M0 hard duration maximum")
        if int(probe.get("video_streams", 0)) < 1 or int(probe.get("audio_streams", 0)) < 1:
            raise InvalidArtifact("rendered media is missing required video/audio streams")
