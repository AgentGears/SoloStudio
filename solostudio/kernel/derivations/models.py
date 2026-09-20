from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PlannedArtifact:
    output_role: str
    capability: str
    kind: str
    input_fingerprint: str
    disposition: str
    artifact_id: str | None = None
    job_id: str | None = None
    attempt_id: str | None = None


@dataclass(frozen=True, slots=True)
class ArtifactRequirement:
    output_role: str
    capability: str
    job_type: str
    kind: str
    media_type: str
    filename: str
    semantic_inputs: dict
    source_object_digests: dict[str, str]
    source_artifacts: tuple[tuple[str, str], ...]
    route: dict
    input_fingerprint: str
