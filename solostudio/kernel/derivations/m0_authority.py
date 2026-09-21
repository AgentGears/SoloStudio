from __future__ import annotations

from typing import Any

from solostudio.kernel.derivations.variant_artifacts import VariantArtifactAuthorityService
from solostudio.kernel.errors import (
    ArtifactDigestMismatch,
    InvalidArtifact,
    MissingRetainedArtifact,
)


class M0ArtifactAuthorityService(VariantArtifactAuthorityService):
    """Final M0 Artifact authority fences shared by revision and variant planners."""

    def find_reusable(
        self,
        production_id: str,
        kind: str,
        expected_fingerprint: str,
    ) -> dict[str, Any] | None:
        """Return only reusable revision-scoped Artifacts for generic derivation inputs."""
        with self.store.read() as db:
            ids = [
                str(row["id"])
                for row in db.execute(
                    """
                    SELECT id
                    FROM artifacts
                    WHERE production_id = ?
                      AND variant_id IS NULL
                      AND kind = ?
                      AND input_fingerprint = ?
                    ORDER BY created_at, id
                    """,
                    (production_id, kind, expected_fingerprint),
                )
            ]
        for artifact_id in ids:
            try:
                artifact = self.artifact(artifact_id, verify_bytes=True)
                payload = self.read_bytes(artifact_id)
                self._validate(str(artifact["kind"]), str(artifact["media_type"]), payload)
                return artifact
            except (InvalidArtifact, MissingRetainedArtifact, ArtifactDigestMismatch):
                continue
        return None

    def _validate_composition_job(
        self,
        db: Any,
        job: dict[str, Any],
        payload: bytes,
        *,
        production_id: str,
        production_revision_id: str,
        variant_id: str,
        artifact_input_fingerprint: Any,
    ) -> None:
        variant, revision_payload = self._variant_context(
            db,
            production_id=production_id,
            production_revision_id=production_revision_id,
            variant_id=variant_id,
        )
        self._validate_variant_against_revision(variant, revision_payload)
        super()._validate_composition_job(
            db,
            job,
            payload,
            production_id=production_id,
            production_revision_id=production_revision_id,
            variant_id=variant_id,
            artifact_input_fingerprint=artifact_input_fingerprint,
        )

    @staticmethod
    def _validate_composition_payload(
        composition: Any,
        *,
        variant: dict[str, Any],
        composition_preferences: dict[str, Any],
        source_digests: dict[str, str],
    ) -> None:
        VariantArtifactAuthorityService._validate_composition_payload(
            composition,
            variant=variant,
            composition_preferences=composition_preferences,
            source_digests=source_digests,
        )
        intent = variant["intent"]
        if composition["duration_ms"] != intent["duration_min_ms"]:
            raise InvalidArtifact(
                "composition payload duration does not match deterministic compiler selection"
            )

    @staticmethod
    def _validate_variant_against_revision(
        variant: dict[str, Any],
        revision_payload: dict[str, Any],
    ) -> None:
        intent = variant.get("intent")
        if not isinstance(intent, dict):
            raise InvalidArtifact("variant intent must be an object")

        brief = revision_payload.get("brief")
        if not isinstance(brief, dict):
            raise InvalidArtifact("captured revision brief must be an object")
        language = brief.get("primary_language")
        if not isinstance(language, str) or not language:
            raise InvalidArtifact("captured revision primary language is invalid")
        if intent.get("language") != language:
            raise InvalidArtifact(
                "M0 variant language adaptation requires a translated revision and is not implicit"
            )

        captions = revision_payload.get("captions")
        if not isinstance(captions, dict) or type(captions.get("enabled", True)) is not bool:
            raise InvalidArtifact("captured caption preferences are invalid")
        if intent.get("caption_mode") == "burned" and captions.get("enabled", True) is not True:
            raise InvalidArtifact("burned-caption variant requires captured captions to be enabled")

        visual_plan = revision_payload.get("visual_plan")
        if not isinstance(visual_plan, list):
            raise InvalidArtifact("captured visual plan must be a list")
