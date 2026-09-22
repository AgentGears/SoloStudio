from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from solostudio.connectors import FakeDestinationConnector
from solostudio.kernel.clock import Clock
from solostudio.kernel.errors import ContractExpired, InvalidCommand, NotFound
from solostudio.kernel.identity import canonical_hash, canonical_text
from solostudio.kernel.ids import IdSource
from solostudio.kernel.store import KernelStore


class DestinationContractService:
    def __init__(self, store: KernelStore, clock: Clock, ids: IdSource) -> None:
        self.store = store
        self.clock = clock
        self.ids = ids

    def ensure_fake_account(self) -> str:
        account_id = FakeDestinationConnector.default_account_id
        now = self.clock.now()
        metadata = canonical_text({"current_contract_version": "fake-v1"})
        with self.store.write() as db:
            existing = db.execute(
                "SELECT connector_type FROM destination_accounts WHERE id=?",
                (account_id,),
            ).fetchone()
            if existing:
                if str(existing["connector_type"]) != FakeDestinationConnector.connector_type:
                    raise RuntimeError("M0 fake destination account id is already owned by another connector")
                return account_id
            db.execute(
                """
                INSERT INTO destination_accounts(
                    id,connector_type,display_name,status,credential_ref,metadata_json,created_at,updated_at
                ) VALUES (?,?,?,'ACTIVE',NULL,?,?,?)
                """,
                (
                    account_id,
                    FakeDestinationConnector.connector_type,
                    "M0 Fake Short Video",
                    metadata,
                    now,
                    now,
                ),
            )
            self._journal(
                db,
                account_id,
                "DESTINATION_ACCOUNT_CREATED",
                {"connector_type": FakeDestinationConnector.connector_type},
            )
        return account_id

    def account(self, account_id: str) -> dict[str, Any]:
        with self.store.read() as db:
            row = db.execute("SELECT * FROM destination_accounts WHERE id=?", (account_id,)).fetchone()
            if not row:
                raise NotFound(f"destination account not found: {account_id}")
            result = dict(row)
        try:
            result["metadata"] = json.loads(str(result.pop("metadata_json")))
        except json.JSONDecodeError as exc:
            raise RuntimeError("destination account metadata is unreadable") from exc
        return result

    def set_fake_contract_version(self, account_id: str, version: str) -> None:
        if version not in FakeDestinationConnector.versions():
            raise InvalidCommand(f"unsupported fake destination contract version: {version}")
        now = self.clock.now()
        with self.store.write() as db:
            row = db.execute(
                "SELECT connector_type,status,metadata_json FROM destination_accounts WHERE id=?",
                (account_id,),
            ).fetchone()
            if not row:
                raise NotFound(f"destination account not found: {account_id}")
            if str(row["connector_type"]) != FakeDestinationConnector.connector_type:
                raise InvalidCommand("destination account is not owned by the M0 fake connector")
            if str(row["status"]) != "ACTIVE":
                raise InvalidCommand("destination account is not active")
            try:
                metadata = json.loads(str(row["metadata_json"]))
            except json.JSONDecodeError as exc:
                raise RuntimeError("destination account metadata is unreadable") from exc
            if not isinstance(metadata, dict):
                raise RuntimeError("destination account metadata must be an object")
            metadata["current_contract_version"] = version
            db.execute(
                "UPDATE destination_accounts SET metadata_json=?,updated_at=? WHERE id=?",
                (canonical_text(metadata), now, account_id),
            )
            self._journal(
                db,
                account_id,
                "DESTINATION_CONTRACT_VERSION_SELECTED",
                {"contract_version": version},
            )

    def discover_contract(self, account_id: str, *, valid_for_seconds: int = 3600) -> dict[str, Any]:
        if type(valid_for_seconds) is not int or valid_for_seconds < 1:
            raise InvalidCommand("contract validity must be a positive integer number of seconds")
        account = self.account(account_id)
        if account["status"] != "ACTIVE":
            raise InvalidCommand("destination account is not active")
        if account["connector_type"] != FakeDestinationConnector.connector_type:
            raise InvalidCommand("M0 supports only the fake destination connector")
        metadata = account["metadata"]
        version = metadata.get("current_contract_version") if isinstance(metadata, dict) else None
        if not isinstance(version, str):
            raise InvalidCommand("destination account has no current contract version")

        contract = FakeDestinationConnector.contract(account_id, version)
        canonical_json = canonical_text(contract)
        fingerprint = canonical_hash(contract)
        now = self.clock.now()
        valid_until = _add_seconds(now, valid_for_seconds)

        with self.store.write() as db:
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
                    "DESTINATION_CONTRACT_REFRESHED",
                    {
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
                "DESTINATION_CONTRACT_DISCOVERED",
                {
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
        account = self.account(account_id)
        if account["status"] != "ACTIVE":
            raise InvalidCommand("destination account is not active")
        metadata = account["metadata"]
        version = metadata.get("current_contract_version") if isinstance(metadata, dict) else None
        if not isinstance(version, str):
            raise InvalidCommand("destination account has no current contract version")
        expected = FakeDestinationConnector.contract(account_id, version)
        fingerprint = canonical_hash(expected)
        with self.store.read() as db:
            row = db.execute(
                """
                SELECT * FROM destination_contract_snapshots
                WHERE destination_account_id=? AND fingerprint=?
                """,
                (account_id, fingerprint),
            ).fetchone()
        if row:
            result = self._snapshot_row(row)
            if not self._is_expired(result):
                return result
            if not refresh:
                raise ContractExpired(f"destination contract snapshot is expired: {result['id']}")
        if not refresh:
            raise ContractExpired("no unexpired destination contract snapshot is available")
        return self.discover_contract(account_id)

    def snapshot(self, snapshot_id: str) -> dict[str, Any]:
        with self.store.read() as db:
            row = db.execute(
                "SELECT * FROM destination_contract_snapshots WHERE id=?",
                (snapshot_id,),
            ).fetchone()
            if not row:
                raise NotFound(f"destination contract snapshot not found: {snapshot_id}")
            return self._snapshot_row(row)

    def is_expired(self, snapshot_id: str) -> bool:
        return self._is_expired(self.snapshot(snapshot_id))

    def _is_expired(self, snapshot: dict[str, Any]) -> bool:
        valid_until = snapshot.get("valid_until")
        if valid_until is None:
            return False
        return _parse_timestamp(self.clock.now()) >= _parse_timestamp(str(valid_until))

    @staticmethod
    def _snapshot_row(row: Any) -> dict[str, Any]:
        result = dict(row)
        try:
            contract = json.loads(str(result["canonical_json"]))
        except json.JSONDecodeError as exc:
            raise RuntimeError("destination contract snapshot JSON is unreadable") from exc
        if not isinstance(contract, dict):
            raise RuntimeError("destination contract snapshot must decode to an object")
        if canonical_text(contract) != str(result["canonical_json"]):
            raise RuntimeError("destination contract snapshot is not canonical")
        if canonical_hash(contract) != str(result["fingerprint"]):
            raise RuntimeError("destination contract snapshot fingerprint is invalid")
        result["contract"] = contract
        return result

    def _journal(self, db: Any, entity_id: str, event_type: str, event: dict[str, Any]) -> None:
        db.execute(
            """
            INSERT INTO journal_entries(
                production_id,entity_type,entity_id,event_type,event_json,created_at
            ) VALUES (NULL,'destination_contract',?,?,?,?)
            """,
            (entity_id, event_type, canonical_text(event), self.clock.now()),
        )


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"invalid persisted timestamp: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _add_seconds(value: str, seconds: int) -> str:
    updated = _parse_timestamp(value) + timedelta(seconds=seconds)
    return updated.isoformat(timespec="milliseconds").replace("+00:00", "Z")
