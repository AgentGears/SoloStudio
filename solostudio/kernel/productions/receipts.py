from __future__ import annotations

import json
from typing import Any

from solostudio.kernel.errors import NotFound
from solostudio.kernel.identity import canonical_hash, canonical_text
from solostudio.kernel.principals import Principal
from solostudio.kernel.productions.models import CommandResult, RevisionResult


class ReceiptMixin:
    def _command_fingerprint(self, principal: Principal, action: str, command_input: dict[str, Any]) -> str:
        return canonical_hash({"principal_type": principal.type.value, "principal_id": principal.id, "action": action, "input": command_input})

    def _current_version(self, db: Any, production_id: str) -> int:
        row = db.execute("SELECT state_version FROM productions WHERE id = ?", (production_id,)).fetchone()
        if not row:
            raise NotFound(f"production not found: {production_id}")
        return int(row["state_version"])

    def _command_from_prior(self, db: Any, prior: Any, fingerprint: str, production_id: str) -> CommandResult:
        current_version = self._current_version(db, production_id)
        affected = json.loads(prior["affected_json"])
        if prior["command_fingerprint"] != fingerprint:
            return CommandResult("IDEMPOTENCY_CONFLICT", str(prior["id"]), str(prior["status"]), current_version, affected, "IDEMPOTENCY_CONFLICT", "idempotency key was already used for a different command")
        return CommandResult("REPLAYED", str(prior["id"]), str(prior["status"]), current_version, affected, prior["error_code"], prior["error_message"])

    def _capture_from_prior(self, db: Any, prior: Any, fingerprint: str, production_id: str) -> RevisionResult | CommandResult:
        current_version = self._current_version(db, production_id)
        affected = json.loads(prior["affected_json"])
        if prior["command_fingerprint"] != fingerprint:
            return CommandResult("IDEMPOTENCY_CONFLICT", str(prior["id"]), str(prior["status"]), current_version, affected, "IDEMPOTENCY_CONFLICT", "idempotency key was already used for a different command")
        if prior["status"] == "COMMITTED" and "revision_id" in affected:
            revision = db.execute("SELECT * FROM production_revisions WHERE id = ?", (affected["revision_id"],)).fetchone()
            if not revision:
                raise RuntimeError("committed capture receipt references missing revision")
            return RevisionResult("REPLAYED", str(prior["id"]), str(revision["id"]), int(revision["sequence"]), str(revision["content_hash"]), int(revision["state_version_at_capture"]))
        return CommandResult("REPLAYED", str(prior["id"]), str(prior["status"]), current_version, affected, prior["error_code"], prior["error_message"])

    def _persist_stale(
        self,
        db: Any,
        principal: Principal,
        production_id: str,
        idempotency_key: str,
        action: str,
        fingerprint: str,
        expected_state_version: int,
        current_version: int,
        now: str,
        event_type: str,
    ) -> CommandResult:
        receipt_id = self._insert_receipt(
            db=db,
            production_id=production_id,
            idempotency_key=idempotency_key,
            principal=principal,
            action=action,
            fingerprint=fingerprint,
            status="REJECTED_STALE",
            expected_state_version=expected_state_version,
            previous_state_version=current_version,
            resulting_state_version=current_version,
            affected={},
            error_code="STALE_COMMAND",
            error_message=f"expected state version {expected_state_version}, current is {current_version}",
            now=now,
        )
        self._journal(db, production_id, "command_receipt", receipt_id, event_type, {"current_state_version": current_version})
        return CommandResult("STALE_COMMAND", receipt_id, "REJECTED_STALE", current_version, {}, "STALE_COMMAND", f"current state version is {current_version}")

    def _insert_receipt(
        self,
        *,
        db: Any,
        production_id: str,
        idempotency_key: str,
        principal: Principal,
        action: str,
        fingerprint: str,
        status: str,
        expected_state_version: int,
        previous_state_version: int,
        resulting_state_version: int,
        affected: dict[str, Any],
        error_code: str | None,
        error_message: str | None,
        now: str,
    ) -> str:
        receipt_id = self.ids.new("rcpt")
        db.execute(
            """
            INSERT INTO production_command_receipts(
                id,production_id,idempotency_key,principal_type,principal_id,action,command_fingerprint,status,
                expected_state_version,previous_state_version,resulting_state_version,affected_json,error_code,error_message,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                receipt_id,
                production_id,
                idempotency_key,
                principal.type.value,
                principal.id,
                action,
                fingerprint,
                status,
                expected_state_version,
                previous_state_version,
                resulting_state_version,
                canonical_text(affected),
                error_code,
                error_message,
                now,
            ),
        )
        return receipt_id

    def _journal(self, db: Any, production_id: str | None, entity_type: str, entity_id: str, event_type: str, event: dict[str, Any]) -> None:
        db.execute(
            "INSERT INTO journal_entries(production_id,entity_type,entity_id,event_type,event_json,created_at) VALUES (?,?,?,?,?,?)",
            (production_id, entity_type, entity_id, event_type, canonical_text(event), self.clock.now()),
        )

