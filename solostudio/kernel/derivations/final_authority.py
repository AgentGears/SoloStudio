from __future__ import annotations

import json
from typing import Any, Iterable

from solostudio.kernel.artifacts import PreparedArtifact
from solostudio.kernel.derivations.m0_authority import M0ArtifactAuthorityService
from solostudio.kernel.derivations.service import DerivationService
from solostudio.kernel.errors import InvalidArtifact, InvalidCommand, NotFound


_VARIANT_OUTPUT_KINDS = {"composition_spec", "rendered_video", "cover_image"}
_DERIVATION_OUTPUT_AUTHORITY = {
    "voice_audio": ("speech.synthesize", "VOICE_SYNTHESIZE", "script_text", "script_text"),
    "caption_track": ("captions.generate", "CAPTIONS_GENERATE", "script_text", "script_text"),
    "visual_image": ("image.generate", "VISUAL_IMAGE_GENERATE", "visual_plan", "visual_plan"),
}
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
        elif prepared.kind in _DERIVATION_OUTPUT_AUTHORITY and (
            producer_job_id is not None or producer_attempt_id is not None
        ):
            self._validate_running_revision_derivation_output(
                db,
                prepared,
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

    def _validate_revision_derivation_producer(
        self,
        db: Any,
        artifact: dict[str, Any],
    ) -> None:
        super()._validate_revision_derivation_producer(db, artifact)
        job_id = artifact.get("producer_job_id")
        if not isinstance(job_id, str) or not job_id:
            raise InvalidArtifact("derivation source is missing producer JobSpec authority")
        job = self._job_authority(db, job_id)
        expected = self._expected_revision_derivation_payload(db, job, str(artifact["kind"]))
        retained = self.objects.read_bytes(
            str(artifact["object_digest"]),
            int(artifact["byte_size"]),
            str(artifact["object_relpath"]),
        )
        if retained != expected:
            raise InvalidArtifact("derivation source bytes do not match deterministic provider output")

    def _validate_running_revision_derivation_output(
        self,
        db: Any,
        prepared: PreparedArtifact,
        *,
        producer_job_id: str | None,
        producer_attempt_id: str | None,
    ) -> None:
        if not isinstance(producer_job_id, str) or not producer_job_id:
            raise InvalidArtifact("derivation output requires producer JobSpec authority")
        if not isinstance(producer_attempt_id, str) or not producer_attempt_id:
            raise InvalidArtifact("derivation output requires a running producer Attempt")

        job = self._job_authority(db, producer_job_id)
        authority = _DERIVATION_OUTPUT_AUTHORITY[prepared.kind]
        capability, job_type, _source_role, _source_kind = authority
        if (
            job["job_class"] != "ARTIFACT"
            or job["job_type"] != job_type
            or job["semantic_capability"] != capability
            or job["variant_id"] is not None
            or job["state"] != "RUNNING"
        ):
            raise InvalidArtifact("derivation output producer JobSpec is not authoritative and running")

        attempt = db.execute(
            "SELECT job_id,state,executor_identity FROM attempts WHERE id=?",
            (producer_attempt_id,),
        ).fetchone()
        if (
            not attempt
            or str(attempt["job_id"]) != producer_job_id
            or attempt["state"] != "RUNNING"
            or attempt["executor_identity"] != "deterministic-artifact-provider"
        ):
            raise InvalidArtifact("derivation output producer Attempt is not the running deterministic provider")
        if prepared.producer_stage != "deterministic_artifact_provider":
            raise InvalidArtifact("derivation output producer stage is not authoritative")

        expected = self._expected_revision_derivation_payload(db, job, prepared.kind)
        retained = self.objects.read_bytes(
            prepared.object_record.digest_sha256,
            prepared.object_record.byte_size,
            prepared.object_record.object_relpath,
        )
        if retained != expected:
            raise InvalidArtifact("derivation output bytes do not match deterministic provider output")

    def _expected_revision_derivation_payload(
        self,
        db: Any,
        job: dict[str, Any],
        kind: str,
    ) -> bytes:
        authority = _DERIVATION_OUTPUT_AUTHORITY.get(kind)
        if authority is None:
            raise InvalidArtifact("derivation output kind has no deterministic provider authority")
        capability, job_type, source_role, source_kind = authority
        if job["job_type"] != job_type or job["semantic_capability"] != capability:
            raise InvalidArtifact("derivation output kind does not match producer capability")

        source_entries = job["spec"].get("source_artifacts")
        if (
            not isinstance(source_entries, list)
            or len(source_entries) != 1
            or not isinstance(source_entries[0], dict)
            or source_entries[0].get("role") != source_role
            or not isinstance(source_entries[0].get("artifact_id"), str)
            or not source_entries[0]["artifact_id"]
        ):
            raise InvalidArtifact("derivation producer source Artifact binding is invalid")
        source = self._artifact_authority(db, str(source_entries[0]["artifact_id"]))
        if (
            str(source["production_id"]) != str(job["production_id"])
            or source["production_revision_id"] != job["production_revision_id"]
            or source["variant_id"] is not None
            or source["kind"] != source_kind
            or source["producer_stage"] != "revision_capture"
            or source["producer_job_id"] is not None
            or source["producer_attempt_id"] is not None
        ):
            raise InvalidArtifact("derivation producer source is not authoritative captured revision input")

        try:
            return DerivationService._render(
                self,
                capability,
                job,
                {source_role: source},
            )
        except (InvalidArtifact, InvalidCommand, KeyError, TypeError) as exc:
            raise InvalidArtifact("deterministic derivation output could not be reconstructed") from exc

    def _source_text(self, sources: dict[str, dict[str, Any]], role: str) -> str:
        source = sources.get(role)
        if source is None:
            raise InvalidCommand(f"Artifact job is missing source role: {role}")
        try:
            return self.objects.read_bytes(
                str(source["object_digest"]),
                int(source["byte_size"]),
                str(source["object_relpath"]),
            ).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InvalidArtifact(f"{role} source is not valid UTF-8") from exc

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
