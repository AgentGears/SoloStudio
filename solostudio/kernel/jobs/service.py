from __future__ import annotations

from pathlib import Path
from typing import Any

from solostudio.kernel.artifacts import ArtifactService
from solostudio.kernel.clock import Clock
from solostudio.kernel.costs import CostPlan, CostService
from solostudio.kernel.errors import InvalidArtifact, InvalidCommand, NotFound
from solostudio.kernel.ids import IdSource
from solostudio.kernel.jobs.admission import AdmissionMixin
from solostudio.kernel.jobs.execution import ExecutionMixin
from solostudio.kernel.jobs.models import JobAdmission
from solostudio.kernel.jobs.recovery import RecoveryMixin
from solostudio.kernel.jobs.safety import SafeExecutionMixin
from solostudio.kernel.productions import ProductionService
from solostudio.kernel.store import KernelStore


class JobService(SafeExecutionMixin, AdmissionMixin, ExecutionMixin, RecoveryMixin):
    def __init__(
        self,
        data_dir: Path,
        store: KernelStore,
        artifacts: ArtifactService,
        productions: ProductionService,
        costs: CostService,
        clock: Clock,
        ids: IdSource,
    ) -> None:
        self.data_dir = data_dir
        self.store = store
        self.artifacts = artifacts
        self.productions = productions
        self.costs = costs
        self.clock = clock
        self.ids = ids
        self.tmp_root = data_dir / "tmp"
        self.tmp_root.mkdir(parents=True, exist_ok=True)

    def admit(
        self,
        *,
        production_id: str,
        job_class: str,
        job_type: str,
        semantic_capability: str,
        spec: dict[str, Any],
        route: dict[str, Any],
        input_fingerprint: str,
        production_revision_id: str | None = None,
        variant_id: str | None = None,
        max_attempts: int = 2,
        cost_plan: CostPlan | None = None,
        expected_source_state_version: int | None = None,
    ) -> JobAdmission:
        if semantic_capability == "cover.produce":
            self._validate_cover_admission_source(
                production_id=production_id,
                production_revision_id=production_revision_id,
                variant_id=variant_id,
                job_class=job_class,
                job_type=job_type,
                spec=spec,
            )
        return super().admit(
            production_id=production_id,
            production_revision_id=production_revision_id,
            variant_id=variant_id,
            job_class=job_class,
            job_type=job_type,
            semantic_capability=semantic_capability,
            spec=spec,
            route=route,
            input_fingerprint=input_fingerprint,
            max_attempts=max_attempts,
            cost_plan=cost_plan,
            expected_source_state_version=expected_source_state_version,
        )

    def _validate_cover_admission_source(
        self,
        *,
        production_id: str,
        production_revision_id: str | None,
        variant_id: str | None,
        job_class: str,
        job_type: str,
        spec: dict[str, Any],
    ) -> None:
        if (
            job_class != "ARTIFACT"
            or job_type != "COVER_PRODUCE"
            or not isinstance(production_revision_id, str)
            or not production_revision_id
            or variant_id is not None
        ):
            raise InvalidCommand("cover.produce requires an unscoped revision Artifact JobSpec")
        if (
            spec.get("output_role") != "cover.primary"
            or spec.get("kind") != "cover_image"
            or spec.get("media_type") != "image/png"
            or spec.get("filename") != "cover.png"
            or spec.get("semantic_inputs") != {"cover_preferences": {}}
        ):
            raise InvalidCommand("cover.produce JobSpec does not match the implemented M0 cover contract")
        source_entries = spec.get("source_artifacts")
        if (
            not isinstance(source_entries, list)
            or len(source_entries) != 1
            or not isinstance(source_entries[0], dict)
            or source_entries[0].get("role") != "selected_visual"
            or not isinstance(source_entries[0].get("artifact_id"), str)
            or not source_entries[0]["artifact_id"]
        ):
            raise InvalidCommand("cover.produce requires exactly one selected_visual Artifact")

        validator = getattr(self.artifacts, "require_revision_derivation_producer", None)
        if not callable(validator):
            raise InvalidCommand("cover source producer authority validation is unavailable")
        try:
            source = validator(str(source_entries[0]["artifact_id"]))
        except (InvalidArtifact, NotFound) as exc:
            raise InvalidCommand("cover source lacks authoritative revision-derivation producer provenance") from exc
        if (
            str(source["production_id"]) != production_id
            or source["production_revision_id"] != production_revision_id
            or source["variant_id"] is not None
            or source["kind"] != "visual_image"
        ):
            raise InvalidCommand("cover source does not match the requested production revision")
        if spec.get("source_object_digests") != {"selected_visual": str(source["object_digest"])}:
            raise InvalidCommand("cover source Object digest does not match the selected visual Artifact")
