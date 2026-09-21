from __future__ import annotations

import json
from typing import Any, Iterable

from solostudio.kernel.artifacts import PreparedArtifact
from solostudio.kernel.derivations.m0_authority import M0ArtifactAuthorityService
from solostudio.kernel.errors import InvalidArtifact, NotFound


_VARIANT_OUTPUT_KINDS = {"composition_spec", "rendered_video", "cover_image"}
_BOUND_EXECUTORS = {
    "artifact-provider-v1": "deterministic-artifact-provider",
    "media-render-v1": "deterministic-media-renderer",
}


class Slice8ArtifactAuthorityService(M0ArtifactAuthorityService):
    """Final Slice 8 authority fences for job-produced variant outputs."""

    def register_prepared_in_tx(
        self,
        db: Any,
        prepared: PreparedArtifact,
        *,
        production_id: str,
        production_revision_id: str | None = None,
        variant_id: str | None = None,
        producer_job_id: str | None = None,
        producer_attempt_id: str | None = None,
        dependencies: Iterable[tuple[str, str]] = (),
    ) -> str:
        if prepared.kind in _VARIANT_OUTPUT_KINDS:
            self._validate_running_producer_attempt(
                db,
                producer_job_id=producer_job_id,
                producer_attempt_id=producer_attempt_id,
            )
        return super().register_prepared_in_tx(
            db,
            prepared,
            production_id=production_id,
            production_revision_id=production_revision_id,
            variant_id=variant_id,
            producer_job_id=producer_job_id,
            producer_attempt_id=producer_attempt_id,
            dependencies=dependencies,
        )

    def require_revision_derivation_producer(self, artifact_id: str) -> dict[str, Any]:
        """Return a revision derivation Artifact only when its successful producer is authoritative."""
        with self.store.read() as db:
            try:
                artifact = self._artifact_authority(db, artifact_id)
            except NotFound as exc:
                raise InvalidArtifact("revision derivation source Artifact is missing") from exc
            self._validate_revision_derivation_producer(db, artifact)
            return artifact

    @staticmethod
    def _validate_running_producer_attempt(
        db: Any,
        *,
        producer_job_id: str | None,
        producer_attempt_id: str | None,
    ) -> None:
        if not isinstance(producer_job_id, str) or not producer_job_id:
            raise InvalidArtifact("variant output requires producer JobSpec authority")
        if not isinstance(producer_attempt_id, str) or not producer_attempt_id:
            raise InvalidArtifact("variant output requires a running producer Attempt")

        job = db.execute(
            "SELECT id,job_class,state,route_json FROM job_specs WHERE id=?",
            (producer_job_id,),
        ).fetchone()
        if not job:
            raise InvalidArtifact("variant output producer JobSpec is missing")
        if job["job_class"] != "ARTIFACT" or job["state"] != "RUNNING":
            raise InvalidArtifact("variant output producer JobSpec is not running")

        attempt = db.execute(
            "SELECT job_id,state,executor_identity FROM attempts WHERE id=?",
            (producer_attempt_id,),
        ).fetchone()
        if (
            not attempt
            or str(attempt["job_id"]) != producer_job_id
            or attempt["state"] != "RUNNING"
        ):
            raise InvalidArtifact("variant output producer Attempt is not running for the producer JobSpec")

        try:
            route = json.loads(str(job["route_json"]))
        except json.JSONDecodeError as exc:
            raise InvalidArtifact("variant output producer route is unreadable") from exc
        if not isinstance(route, dict):
            raise InvalidArtifact("variant output producer route must be an object")
        expected_executor = _BOUND_EXECUTORS.get(route.get("tool_profile"))
        if expected_executor is None:
            raise InvalidArtifact("variant output producer route has no bound executor")
        if attempt["executor_identity"] != expected_executor:
            raise InvalidArtifact("variant output producer Attempt executor does not match the bound route")
