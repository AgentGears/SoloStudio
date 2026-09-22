from __future__ import annotations

import json
from typing import Any

from solostudio.connectors import FakeDestinationConnector
from solostudio.kernel.destinations.service import DestinationContractService, _add_seconds
from solostudio.kernel.errors import ContractExpired, InvalidCommand, NotFound
from solostudio.kernel.identity import canonical_hash, canonical_text


class Slice9DestinationContractService(DestinationContractService):
    """Final Slice 9 contract observation/freshness authority."""

    def discover_contract(self, account_id: str, *, valid_for_seconds: int = 3600) -> dict[str, Any]:
        if type(valid_for_seconds) is not int or valid_for_seconds < 1:
            raise InvalidCommand("contract validity must be a positive integer number of seconds")
        now = self.clock.now()
        valid_until = _add_seconds(now, valid_for_seconds)

        with self.store.write() as db:
            account = db.execute(
                "SELECT connector_type,status,metadata_json FROM destination_accounts WHERE id=?",
                (account_id,),
            ).fetchone()
            if not account:
                raise NotFound(f"destination account not found: {account_id}")
            if str(account["status"]) != "ACTIVE":
                raise InvalidCommand("destination account is not active")
            if str(account["connector_type"]) != FakeDestinationConnector.connector_type:
                raise InvalidCommand("M0 supports only the fake destination connector")
            try:
                metadata = json.loads(str(account["metadata_json"]))
            except json.JSONDecodeError as exc:
                raise RuntimeError("destination account metadata is unreadable") from exc
            version = metadata.get("current_contract_version") if isinstance(metadata, dict) else None
            if not isinstance(version, str):
                raise InvalidCommand("destination account has no current contract version")

            contract = FakeDestinationConnector.get_destination_contract(account_id, version)
            canonical_json = canonical_text(contract)
            fingerprint = canonical_hash(contract)
            existing = db.execute(
                """
                SELECT * FROM destination_contract_snapshots
                WHERE destination_account_id=? AND fingerprint=?
                """,
                (account_id, fingerprint),
            ).fetchone()
            if existing:
                result = self._snapshot_row(existing)
                if result["canonical_json"] != canonical_json:
                    raise RuntimeError("destination contract fingerprint collision detected")
                if not self._is_expired(result):
                    return result
                db.execute(
                    """
                    UPDATE destination_contract_snapshots
                    SET discovered_at=?,valid_until=?
                    WHERE id=?
                    """,
                    (now, valid_until, result["id"]),
                )
                self._journal(
                    db,
                    str(result["id"]),
                    "DESTINATION_CONTRACT_SNAPSHOTTED",
                    {
                        "observation": "REFRESHED",
                        "destination_account_id": account_id,
                        "fingerprint": fingerprint,
                        "contract_version": version,
                        "valid_until": valid_until,
                    },
                )
                refreshed = db.execute(
                    "SELECT * FROM destination_contract_snapshots WHERE id=?",
                    (result["id"],),
                ).fetchone()
                assert refreshed is not None
                return self._snapshot_row(refreshed)

            snapshot_id = self.ids.new("contract")
            db.execute(
                """
                INSERT INTO destination_contract_snapshots(
                    id,destination_account_id,fingerprint,canonical_json,discovered_at,valid_until
                ) VALUES (?,?,?,?,?,?)
                """,
                (snapshot_id, account_id, fingerprint, canonical_json, now, valid_until),
            )
            self._journal(
                db,
                snapshot_id,
                "DESTINATION_CONTRACT_SNAPSHOTTED",
                {
                    "observation": "DISCOVERED",
                    "destination_account_id": account_id,
                    "fingerprint": fingerprint,
                    "contract_version": version,
                    "valid_until": valid_until,
                },
            )
            row = db.execute(
                "SELECT * FROM destination_contract_snapshots WHERE id=?",
                (snapshot_id,),
            ).fetchone()
            assert row is not None
            return self._snapshot_row(row)

    def current_contract(self, account_id: str, *, refresh: bool = True) -> dict[str, Any]:
        if refresh:
            return self.discover_contract(account_id)

        with self.store.read() as db:
            account = db.execute(
                "SELECT connector_type,status,metadata_json FROM destination_accounts WHERE id=?",
                (account_id,),
            ).fetchone()
            if not account:
                raise NotFound(f"destination account not found: {account_id}")
            if str(account["status"]) != "ACTIVE":
                raise InvalidCommand("destination account is not active")
            if str(account["connector_type"]) != FakeDestinationConnector.connector_type:
                raise InvalidCommand("M0 supports only the fake destination connector")
            try:
                metadata = json.loads(str(account["metadata_json"]))
            except json.JSONDecodeError as exc:
                raise RuntimeError("destination account metadata is unreadable") from exc
            version = metadata.get("current_contract_version") if isinstance(metadata, dict) else None
            if not isinstance(version, str):
                raise InvalidCommand("destination account has no current contract version")
            expected = FakeDestinationConnector.get_destination_contract(account_id, version)
            fingerprint = canonical_hash(expected)
            row = db.execute(
                """
                SELECT * FROM destination_contract_snapshots
                WHERE destination_account_id=? AND fingerprint=?
                """,
                (account_id, fingerprint),
            ).fetchone()
            if not row:
                raise ContractExpired("no unexpired destination contract snapshot is available")
            result = self._snapshot_row(row)
            if self._is_expired(result):
                raise ContractExpired(f"destination contract snapshot is expired: {result['id']}")
            return result

    @staticmethod
    def _snapshot_row(row: Any) -> dict[str, Any]:
        result = DestinationContractService._snapshot_row(row)
        contract = result["contract"]
        account_id = str(result["destination_account_id"])
        if contract.get("account_id") != account_id:
            raise RuntimeError("destination contract account identity does not match its snapshot row")
        if contract.get("destination") != FakeDestinationConnector.connector_type:
            raise RuntimeError("destination contract connector identity is invalid")
        version = contract.get("contract_version")
        if version not in FakeDestinationConnector.versions():
            raise RuntimeError("destination contract version is unsupported by the M0 fake connector")
        expected = FakeDestinationConnector.get_destination_contract(account_id, str(version))
        if contract != expected:
            raise RuntimeError("destination contract snapshot does not match connector authority")
        return result
