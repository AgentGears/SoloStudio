from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from solostudio.kernel.clock import Clock
from solostudio.kernel.destinations import DestinationContractService
from solostudio.kernel.errors import InvalidArtifact, InvalidCommand, NotFound, VariantRequired
from solostudio.kernel.identity import canonical_hash, canonical_text
from solostudio.kernel.ids import IdSource
from solostudio.kernel.store import KernelStore
from solostudio.kernel.variants import VariantService


@dataclass(frozen=True, slots=True)
class ContractRevalidation:
    disposition: str
    current_contract_id: str
    current_contract_fingerprint: str


class PackagingService:
    def __init__(
        self,
        store: KernelStore,
        clock: Clock,
        ids: IdSource,
        artifacts: Any,
        variants: VariantService,
        destinations: DestinationContractService,
    ) -> None:
        self.store = store
        self.clock = clock
        self.ids = ids
        self.artifacts = artifacts
        self.variants = variants
        self.destinations = destinations

    def build_package(
        self,
        *,
        variant_id: str,
        destination_account_id: str,
        render_artifact_id: str,
        metadata: dict[str, Any],
        settings: dict[str, Any],
    ) -> str:
        variant = self.variants.variant(variant_id)
        if variant["state"] != "READY":
            raise InvalidCommand("package requires a READY DeliveryVariant")
        contract_snapshot = self.destinations.current_contract(destination_account_id, refresh=True)
        contract = contract_snapshot["contract"]

        artifact = self.artifacts.artifact(render_artifact_id, verify_bytes=True)
        self._validate_render_binding(variant, artifact)
        self._validate_material(contract, variant, artifact)
        normalized_metadata = self._validate_metadata(contract, metadata)
        normalized_settings = self._validate_settings(contract, settings)

        package = {
            "schema_version": 1,
            "production_id": str(variant["production_id"]),
            "production_revision_id": str(variant["source_revision_id"]),
            "variant_id": variant_id,
            "destination_account_id": destination_account_id,
            "destination_contract_fingerprint": str(contract_snapshot["fingerprint"]),
            "media": [
                {
                    "artifact_id": render_artifact_id,
                    "sha256": str(artifact["object_digest"]),
                    "media_type": str(artifact["media_type"]),
                    "byte_size": int(artifact["byte_size"]),
                }
            ],
            "metadata": normalized_metadata,
            "settings": normalized_settings,
        }
        canonical_json = canonical_text(package)
        package_hash = canonical_hash(package)
        now = self.clock.now()

        with self.store.write() as db:
            existing = db.execute(
                "SELECT id,canonical_json FROM package_revisions WHERE production_id=? AND canonical_hash=?",
                (variant["production_id"], package_hash),
            ).fetchone()
            if existing:
                if str(existing["canonical_json"]) != canonical_json:
                    raise RuntimeError("package canonical hash collision detected")
                return str(existing["id"])

            package_id = self.ids.new("pkg")
            db.execute(
                """
                INSERT INTO package_revisions(
                    id,production_id,variant_id,destination_account_id,
                    destination_contract_id,destination_contract_fingerprint,
                    canonical_json,canonical_hash,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    package_id,
                    variant["production_id"],
                    variant_id,
                    destination_account_id,
                    contract_snapshot["id"],
                    contract_snapshot["fingerprint"],
                    canonical_json,
                    package_hash,
                    now,
                ),
            )
            self._journal(
                db,
                str(variant["production_id"]),
                "package_revision",
                package_id,
                "PACKAGE_REVISION_CREATED",
                {
                    "variant_id": variant_id,
                    "destination_account_id": destination_account_id,
                    "destination_contract_fingerprint": contract_snapshot["fingerprint"],
                    "canonical_hash": package_hash,
                },
            )
            return package_id

    def package(self, package_id: str) -> dict[str, Any]:
        with self.store.read() as db:
            row = db.execute("SELECT * FROM package_revisions WHERE id=?", (package_id,)).fetchone()
            if not row:
                raise NotFound(f"package revision not found: {package_id}")
            result = dict(row)
        package = _load_canonical(
            str(result["canonical_json"]),
            str(result["canonical_hash"]),
            "package revision",
        )
        self._validate_package_row(result, package)

        variant = self.variants.variant(str(result["variant_id"]))
        if (
            str(variant["production_id"]) != str(result["production_id"])
            or str(variant["source_revision_id"]) != str(package["production_revision_id"])
        ):
            raise RuntimeError("package DeliveryVariant lineage is inconsistent")

        contract_snapshot = self.destinations.snapshot(str(result["destination_contract_id"]))
        if (
            str(contract_snapshot["destination_account_id"]) != str(result["destination_account_id"])
            or str(contract_snapshot["fingerprint"])
            != str(result["destination_contract_fingerprint"])
        ):
            raise RuntimeError("package destination contract lineage is inconsistent")

        media = package["media"][0]
        artifact = self.artifacts.artifact(str(media["artifact_id"]), verify_bytes=True)
        self._validate_render_binding(variant, artifact)
        if (
            str(artifact["object_digest"]) != str(media["sha256"])
            or str(artifact["media_type"]) != str(media["media_type"])
            or int(artifact["byte_size"]) != int(media["byte_size"])
        ):
            raise InvalidArtifact("PackageRevision media identity does not match retained Artifact bytes")

        result["package"] = package
        return result

    def build_envelope(
        self,
        *,
        package_revision_id: str,
        publication_intent_id: str,
        scheduled_for: str | None = None,
        cost_ceiling_microunits: int = 0,
    ) -> str:
        if not isinstance(publication_intent_id, str) or not publication_intent_id.strip():
            raise InvalidCommand("publication_intent_id must be a non-empty string")
        publication_intent_id = publication_intent_id.strip()
        if type(cost_ceiling_microunits) is not int or cost_ceiling_microunits < 0:
            raise InvalidCommand("cost ceiling must be a non-negative integer microunit value")
        if scheduled_for is not None:
            if not isinstance(scheduled_for, str) or not scheduled_for.strip():
                raise InvalidCommand("scheduled_for must be null or an ISO-8601 timestamp")
            _validate_timestamp(scheduled_for)

        package_row = self.package(package_revision_id)
        package = package_row["package"]
        envelope = self._expected_envelope(
            package_revision_id=package_revision_id,
            package=package,
            publication_intent_id=publication_intent_id,
            scheduled_for=scheduled_for,
            cost_ceiling_microunits=cost_ceiling_microunits,
        )
        canonical_json = canonical_text(envelope)
        envelope_hash = canonical_hash(envelope)
        now = self.clock.now()

        with self.store.write() as db:
            existing = db.execute(
                "SELECT id,canonical_json,canonical_hash FROM publication_envelopes WHERE publication_intent_id=?",
                (publication_intent_id,),
            ).fetchone()
            if existing:
                if (
                    str(existing["canonical_json"]) != canonical_json
                    or str(existing["canonical_hash"]) != envelope_hash
                ):
                    raise InvalidCommand(
                        "publication_intent_id is already bound to a different immutable envelope"
                    )
                return str(existing["id"])

            envelope_id = self.ids.new("env")
            db.execute(
                """
                INSERT INTO publication_envelopes(
                    id,package_revision_id,publication_intent_id,canonical_json,canonical_hash,created_at
                ) VALUES (?,?,?,?,?,?)
                """,
                (
                    envelope_id,
                    package_revision_id,
                    publication_intent_id,
                    canonical_json,
                    envelope_hash,
                    now,
                ),
            )
            self._journal(
                db,
                str(package["production_id"]),
                "publication_envelope",
                envelope_id,
                "PUBLICATION_ENVELOPE_CREATED",
                {
                    "package_revision_id": package_revision_id,
                    "publication_intent_id": publication_intent_id,
                    "canonical_hash": envelope_hash,
                },
            )
            return envelope_id

    def envelope(self, envelope_id: str) -> dict[str, Any]:
        with self.store.read() as db:
            row = db.execute("SELECT * FROM publication_envelopes WHERE id=?", (envelope_id,)).fetchone()
            if not row:
                raise NotFound(f"publication envelope not found: {envelope_id}")
            result = dict(row)
        envelope = _load_canonical(
            str(result["canonical_json"]),
            str(result["canonical_hash"]),
            "publication envelope",
        )
        if set(envelope) != {
            "schema_version",
            "publication_intent_id",
            "package_revision_id",
            "destination_account_id",
            "artifact_digests",
            "title",
            "description",
            "settings",
            "scheduled_for",
            "cost_ceiling_microunits",
            "built_contract_fingerprint",
        }:
            raise RuntimeError("publication envelope canonical fields are invalid")
        if envelope.get("schema_version") != 1:
            raise RuntimeError("publication envelope schema version is invalid")
        if envelope.get("publication_intent_id") != result["publication_intent_id"]:
            raise RuntimeError("publication envelope intent identity does not match its row")
        if envelope.get("package_revision_id") != result["package_revision_id"]:
            raise RuntimeError("publication envelope package identity does not match its row")
        package_row = self.package(str(result["package_revision_id"]))
        expected = self._expected_envelope(
            package_revision_id=str(result["package_revision_id"]),
            package=package_row["package"],
            publication_intent_id=str(result["publication_intent_id"]),
            scheduled_for=envelope.get("scheduled_for"),
            cost_ceiling_microunits=envelope.get("cost_ceiling_microunits"),
        )
        if envelope != expected:
            raise RuntimeError("publication envelope no longer matches its immutable package authority")
        result["envelope"] = envelope
        return result

    def review_payload(self, envelope_id: str) -> dict[str, Any]:
        envelope_row = self.envelope(envelope_id)
        envelope = envelope_row["envelope"]
        package_row = self.package(str(envelope_row["package_revision_id"]))
        package = package_row["package"]
        account = self.destinations.account(str(package["destination_account_id"]))

        media_reviews: list[dict[str, Any]] = []
        for media in package["media"]:
            artifact_id = str(media["artifact_id"])
            artifact = self.artifacts.artifact(artifact_id, verify_bytes=True)
            if (
                str(artifact["object_digest"]) != str(media["sha256"])
                or int(artifact["byte_size"]) != int(media["byte_size"])
                or str(artifact["media_type"]) != str(media["media_type"])
            ):
                raise InvalidArtifact("review media no longer matches PackageRevision byte identity")
            media_reviews.append(
                {
                    "artifact_id": artifact_id,
                    "sha256": str(media["sha256"]),
                    "media_type": str(media["media_type"]),
                    "byte_size": int(media["byte_size"]),
                    "preview_bytes": self.artifacts.read_bytes(artifact_id),
                }
            )

        return {
            "envelope_id": envelope_id,
            "envelope_hash": str(envelope_row["canonical_hash"]),
            "envelope_canonical_bytes": str(envelope_row["canonical_json"]).encode("utf-8"),
            "publication_intent_id": str(envelope["publication_intent_id"]),
            "destination": {
                "account_id": str(account["id"]),
                "display_name": str(account["display_name"]),
                "connector_type": str(account["connector_type"]),
            },
            "title": str(envelope["title"]),
            "description": str(envelope["description"]),
            "settings": dict(envelope["settings"]),
            "scheduled_for": envelope["scheduled_for"],
            "artifact_digests": list(envelope["artifact_digests"]),
            "media": media_reviews,
        }

    def revalidate_envelope(self, envelope_id: str) -> ContractRevalidation:
        envelope_row = self.envelope(envelope_id)
        envelope = envelope_row["envelope"]
        package_row = self.package(str(envelope_row["package_revision_id"]))
        package = package_row["package"]
        current = self.destinations.current_contract(str(package["destination_account_id"]), refresh=True)
        contract = current["contract"]

        variant = self.variants.variant(str(package["variant_id"]))
        media = package["media"]
        artifact = self.artifacts.artifact(str(media[0]["artifact_id"]), verify_bytes=True)
        self._validate_render_binding(variant, artifact)
        try:
            self._validate_material(contract, variant, artifact)
        except VariantRequired:
            return ContractRevalidation(
                "VARIANT_REQUIRED",
                str(current["id"]),
                str(current["fingerprint"]),
            )

        try:
            self._validate_metadata(contract, package["metadata"])
            self._validate_settings(contract, package["settings"])
        except InvalidCommand:
            return ContractRevalidation(
                "PACKAGE_REQUIRED",
                str(current["id"]),
                str(current["fingerprint"]),
            )

        expected = self._expected_envelope(
            package_revision_id=str(envelope_row["package_revision_id"]),
            package=package,
            publication_intent_id=str(envelope["publication_intent_id"]),
            scheduled_for=envelope["scheduled_for"],
            cost_ceiling_microunits=envelope["cost_ceiling_microunits"],
        )
        if expected != envelope:
            raise RuntimeError("stored publication envelope is not reconstructible from its package")
        return ContractRevalidation(
            "UNCHANGED_VALID",
            str(current["id"]),
            str(current["fingerprint"]),
        )

    @staticmethod
    def _validate_render_binding(variant: dict[str, Any], artifact: dict[str, Any]) -> None:
        if (
            str(artifact["production_id"]) != str(variant["production_id"])
            or artifact["production_revision_id"] != variant["source_revision_id"]
            or artifact["variant_id"] != variant["id"]
            or artifact["kind"] != "rendered_video"
            or artifact["media_type"] != "video/mp4"
        ):
            raise InvalidArtifact("package media is not the rendered video for the bound DeliveryVariant")

    @staticmethod
    def _validate_material(
        contract: dict[str, Any],
        variant: dict[str, Any],
        artifact: dict[str, Any],
    ) -> None:
        supported = contract.get("supported_content_types")
        if not isinstance(supported, list) or "video" not in supported:
            raise VariantRequired("destination no longer accepts the M0 video content type")
        media = contract.get("media")
        video = media.get("video") if isinstance(media, dict) else None
        if not isinstance(video, dict):
            raise VariantRequired("destination contract has no usable video material contract")
        accepted = video.get("accepted_media_types")
        max_items = video.get("max_items")
        allowed_aspects = video.get("allowed_aspect_ratios")
        min_duration = video.get("min_duration_ms")
        max_duration = video.get("max_duration_ms")
        if (
            not isinstance(accepted, list)
            or str(artifact["media_type"]) not in accepted
            or type(max_items) is not int
            or max_items < 1
            or not isinstance(allowed_aspects, list)
            or variant["intent"].get("aspect_ratio") not in allowed_aspects
            or type(min_duration) is not int
            or type(max_duration) is not int
            or min_duration < 0
            or max_duration < min_duration
        ):
            raise VariantRequired("destination material contract requires a different DeliveryVariant")
        metadata = artifact.get("metadata")
        probe = metadata.get("validator_result") if isinstance(metadata, dict) else None
        duration = probe.get("duration_ms") if isinstance(probe, dict) else None
        if type(duration) is not int or not (min_duration <= duration <= max_duration):
            raise VariantRequired("rendered media duration does not satisfy destination material contract")

    @staticmethod
    def _validate_metadata(contract: dict[str, Any], metadata: dict[str, Any]) -> dict[str, str]:
        if not isinstance(metadata, dict) or set(metadata) != {"title", "description"}:
            raise InvalidCommand("M0 package metadata requires exactly title and description")
        title = metadata.get("title")
        description = metadata.get("description")
        if not isinstance(title, str) or not isinstance(description, str):
            raise InvalidCommand("package title and description must be strings")
        text_contract = contract.get("text")
        if not isinstance(text_contract, dict):
            raise InvalidCommand("destination text contract is invalid")
        title_max = text_contract.get("title_max")
        description_max = text_contract.get("description_max")
        if type(title_max) is not int or type(description_max) is not int or title_max < 0 or description_max < 0:
            raise InvalidCommand("destination text limits are invalid")
        if len(title) > title_max:
            raise InvalidCommand("package title exceeds destination contract")
        if len(description) > description_max:
            raise InvalidCommand("package description exceeds destination contract")
        return {"title": title, "description": description}

    @staticmethod
    def _validate_settings(contract: dict[str, Any], settings: dict[str, Any]) -> dict[str, str]:
        if not isinstance(settings, dict) or set(settings) != {"visibility"}:
            raise InvalidCommand("M0 package settings require exactly visibility")
        visibility = settings.get("visibility")
        schema = contract.get("settings_schema")
        field = schema.get("visibility") if isinstance(schema, dict) else None
        if not isinstance(field, dict) or field.get("type") != "enum" or field.get("required") is not True:
            raise InvalidCommand("destination visibility contract is invalid")
        values = field.get("values")
        if not isinstance(visibility, str) or not isinstance(values, list) or visibility not in values:
            raise InvalidCommand("package visibility is not accepted by destination contract")
        return {"visibility": visibility}

    @staticmethod
    def _expected_envelope(
        *,
        package_revision_id: str,
        package: dict[str, Any],
        publication_intent_id: str,
        scheduled_for: str | None,
        cost_ceiling_microunits: int,
    ) -> dict[str, Any]:
        if type(cost_ceiling_microunits) is not int or cost_ceiling_microunits < 0:
            raise InvalidCommand("stored cost ceiling is invalid")
        if scheduled_for is not None:
            if not isinstance(scheduled_for, str):
                raise InvalidCommand("stored scheduled_for value is invalid")
            _validate_timestamp(scheduled_for)
        media = package.get("media")
        metadata = package.get("metadata")
        settings = package.get("settings")
        if (
            not isinstance(media, list)
            or len(media) != 1
            or not isinstance(media[0], dict)
            or not isinstance(metadata, dict)
            or not isinstance(settings, dict)
        ):
            raise RuntimeError("package is not usable as an M0 publication envelope source")
        return {
            "schema_version": 1,
            "publication_intent_id": publication_intent_id,
            "package_revision_id": package_revision_id,
            "destination_account_id": package["destination_account_id"],
            "artifact_digests": [media[0]["sha256"]],
            "title": metadata["title"],
            "description": metadata["description"],
            "settings": settings,
            "scheduled_for": scheduled_for,
            "cost_ceiling_microunits": cost_ceiling_microunits,
            "built_contract_fingerprint": package["destination_contract_fingerprint"],
        }

    @staticmethod
    def _validate_package_row(row: dict[str, Any], package: dict[str, Any]) -> None:
        expected_fields = {
            "schema_version",
            "production_id",
            "production_revision_id",
            "variant_id",
            "destination_account_id",
            "destination_contract_fingerprint",
            "media",
            "metadata",
            "settings",
        }
        if set(package) != expected_fields or package.get("schema_version") != 1:
            raise RuntimeError("package revision canonical fields are invalid")
        if package.get("production_id") != row["production_id"]:
            raise RuntimeError("package production identity does not match its row")
        if package.get("variant_id") != row["variant_id"]:
            raise RuntimeError("package variant identity does not match its row")
        if package.get("destination_account_id") != row["destination_account_id"]:
            raise RuntimeError("package destination account does not match its row")
        if package.get("destination_contract_fingerprint") != row["destination_contract_fingerprint"]:
            raise RuntimeError("package destination contract fingerprint does not match its row")
        production_revision_id = package.get("production_revision_id")
        if not isinstance(production_revision_id, str) or not production_revision_id:
            raise RuntimeError("package production revision identity is invalid")
        media = package.get("media")
        if not isinstance(media, list) or len(media) != 1 or not isinstance(media[0], dict):
            raise RuntimeError("M0 package requires exactly one media item")
        if set(media[0]) != {"artifact_id", "sha256", "media_type", "byte_size"}:
            raise RuntimeError("package media identity fields are invalid")
        if (
            not isinstance(media[0].get("artifact_id"), str)
            or not media[0]["artifact_id"]
            or not _is_sha256(media[0].get("sha256"))
            or not isinstance(media[0].get("media_type"), str)
            or type(media[0].get("byte_size")) is not int
            or media[0]["byte_size"] < 0
        ):
            raise RuntimeError("package media identity values are invalid")
        if not isinstance(package.get("metadata"), dict) or not isinstance(package.get("settings"), dict):
            raise RuntimeError("package metadata/settings structure is invalid")

    def _journal(
        self,
        db: Any,
        production_id: str,
        entity_type: str,
        entity_id: str,
        event_type: str,
        event: dict[str, Any],
    ) -> None:
        db.execute(
            """
            INSERT INTO journal_entries(
                production_id,entity_type,entity_id,event_type,event_json,created_at
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                production_id,
                entity_type,
                entity_id,
                event_type,
                canonical_text(event),
                self.clock.now(),
            ),
        )


def _load_canonical(canonical_json: str, expected_hash: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(canonical_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} canonical JSON is unreadable") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} canonical JSON must decode to an object")
    if canonical_text(value) != canonical_json:
        raise RuntimeError(f"{label} canonical JSON is not canonical")
    if canonical_hash(value) != expected_hash:
        raise RuntimeError(f"{label} canonical hash is invalid")
    return value


def _validate_timestamp(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidCommand("scheduled_for must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise InvalidCommand("scheduled_for must include an explicit timezone offset")


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )
