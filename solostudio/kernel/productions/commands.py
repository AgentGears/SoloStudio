from __future__ import annotations

import json
from typing import Any

from solostudio.kernel.errors import InvalidCommand, NotFound
from solostudio.kernel.identity import canonical_text
from solostudio.kernel.principals import Principal
from solostudio.kernel.productions.models import CommandResult
from solostudio.kernel.productions.state import reduce_state


class CommandMixin:
    def handle_command(
        self,
        *,
        principal: Principal,
        production_id: str,
        expected_state_version: int,
        idempotency_key: str,
        action: str,
        command_input: dict[str, Any],
    ) -> CommandResult:
        fingerprint = self._command_fingerprint(principal, action, command_input)
        now = self.clock.now()
        with self.store.write() as db:
            prior = db.execute(
                "SELECT * FROM production_command_receipts WHERE production_id = ? AND idempotency_key = ?",
                (production_id, idempotency_key),
            ).fetchone()
            if prior:
                return self._command_from_prior(db, prior, fingerprint, production_id)

            row = db.execute(
                """
                SELECT p.state_version AS production_version,w.state_version AS working_version,w.state_json
                FROM productions p JOIN working_states w ON w.production_id = p.id
                WHERE p.id = ?
                """,
                (production_id,),
            ).fetchone()
            if not row:
                raise NotFound(f"production not found: {production_id}")
            if row["production_version"] != row["working_version"]:
                raise RuntimeError("working state version invariant violated")
            current_version = int(row["production_version"])
            if current_version != expected_state_version:
                return self._persist_stale(
                    db,
                    principal,
                    production_id,
                    idempotency_key,
                    action,
                    fingerprint,
                    expected_state_version,
                    current_version,
                    now,
                    "COMMAND_REJECTED_STALE",
                )

            state = json.loads(row["state_json"])
            try:
                new_state, changed_paths = reduce_state(state, action, command_input)
            except InvalidCommand as exc:
                receipt_id = self._insert_receipt(
                    db=db,
                    production_id=production_id,
                    idempotency_key=idempotency_key,
                    principal=principal,
                    action=action,
                    fingerprint=fingerprint,
                    status="REJECTED_INVALID",
                    expected_state_version=expected_state_version,
                    previous_state_version=current_version,
                    resulting_state_version=current_version,
                    affected={},
                    error_code=exc.code,
                    error_message=str(exc),
                    now=now,
                )
                self._journal(db, production_id, "command_receipt", receipt_id, "COMMAND_REJECTED_INVALID", {"error_code": exc.code})
                return CommandResult("REJECTED_INVALID", receipt_id, "REJECTED_INVALID", current_version, {}, exc.code, str(exc))

            new_version = current_version + 1
            db.execute(
                "UPDATE working_states SET state_version = ?,state_json = ?,updated_at = ? WHERE production_id = ?",
                (new_version, canonical_text(new_state), now, production_id),
            )
            db.execute("UPDATE productions SET state_version = ?,updated_at = ? WHERE id = ?", (new_version, now, production_id))
            affected = {"changed_paths": changed_paths}
            receipt_id = self._insert_receipt(
                db=db,
                production_id=production_id,
                idempotency_key=idempotency_key,
                principal=principal,
                action=action,
                fingerprint=fingerprint,
                status="COMMITTED",
                expected_state_version=expected_state_version,
                previous_state_version=current_version,
                resulting_state_version=new_version,
                affected=affected,
                error_code=None,
                error_message=None,
                now=now,
            )
            self._journal(db, production_id, "command_receipt", receipt_id, "COMMAND_COMMITTED", {"state_version": new_version, "changed_paths": changed_paths})
            return CommandResult("COMMITTED", receipt_id, "COMMITTED", new_version, affected)
