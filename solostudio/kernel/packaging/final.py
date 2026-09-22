from __future__ import annotations

from typing import Any

from solostudio.kernel.errors import InvalidCommand
from solostudio.kernel.identity import canonical_hash, canonical_text
from solostudio.kernel.packaging.service import PackagingService


class Slice9PackagingService(PackagingService):
    """Final Slice 9 package/envelope authority over destination-facing semantics."""

    def build_package(
        self,
        *,
        variant_id: str,
        destination_account_id: str,
        render_artifact_id: str,
        metadata: dict[str, Any],
        settings: dict[str, Any],
        scheduled_for: str | None = None,
    ) -> str:
        if scheduled_for is not None:
            if not isinstance(scheduled_for, str) or not scheduled_for.strip():
                raise InvalidCommand("scheduled_for must be null or an ISO-8601 timestamp")
            from solostudio.kernel.packaging.service import _validate_timestamp

            _validate_timestamp(scheduled_for)

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
            "scheduled_for": scheduled_for,
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
                "PACKAGE_CREATED",
                {
                    "variant_id": variant_id,
                    "destination_account_id": destination_account_id,
                    "destination_contract_fingerprint": contract_snapshot["fingerprint"],
                    "canonical_hash": package_hash,
                },
            )
            return package_id

    def package(self, package_id: str) -> dict[str, Any]:
        result = super().package(package_id)
        package = result["package"]
        contract = self.destinations.snapshot(str(result["destination_contract_id"]))["contract"]
        variant = self.variants.variant(str(result["variant_id"]))
        media = package["media"]
        artifact = self.artifacts.artifact(str(media[0]["artifact_id"]), verify_bytes=True)
        self._validate_material(contract, variant, artifact)
        self._validate_metadata(contract, package["metadata"])
        self._validate_settings(contract, package["settings"])
        scheduled_for = package["scheduled_for"]
        if scheduled_for is not None:
            from solostudio.kernel.packaging.service import _validate_timestamp

            if not isinstance(scheduled_for, str):
                raise RuntimeError("package scheduled_for value is invalid")
            _validate_timestamp(scheduled_for)
        return result

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
            "scheduled_for",
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
        revision_id = package.get("production_revision_id")
        if not isinstance(revision_id, str) or not revision_id:
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

    @staticmethod
    def _expected_envelope(
        *,
        package_revision_id: str,
        package: dict[str, Any],
        publication_intent_id: str,
        scheduled_for: str | None,
        cost_ceiling_microunits: int,
    ) -> dict[str, Any]:
        package_schedule = package.get("scheduled_for")
        if scheduled_for is not None and scheduled_for != package_schedule:
            raise InvalidCommand("PublicationEnvelope schedule must equal its PackageRevision schedule")
        return PackagingService._expected_envelope(
            package_revision_id=package_revision_id,
            package=package,
            publication_intent_id=publication_intent_id,
            scheduled_for=package_schedule,
            cost_ceiling_microunits=cost_ceiling_microunits,
        )

    def _journal(
        self,
        db: Any,
        production_id: str,
        entity_type: str,
        entity_id: str,
        event_type: str,
        event: dict[str, Any],
    ) -> None:
        event_type = {
            "PACKAGE_REVISION_CREATED": "PACKAGE_CREATED",
            "PUBLICATION_ENVELOPE_CREATED": "ENVELOPE_CREATED",
        }.get(event_type, event_type)
        super()._journal(db, production_id, entity_type, entity_id, event_type, event)


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )
