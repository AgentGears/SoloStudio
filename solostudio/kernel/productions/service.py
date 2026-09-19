from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from solostudio.kernel.clock import Clock
from solostudio.kernel.errors import InvalidCommand, NotFound
from solostudio.kernel.identity import canonical_hash, canonical_text
from solostudio.kernel.ids import IdSource
from solostudio.kernel.principals import Principal
from solostudio.kernel.store import KernelStore
from solostudio.kernel.productions.state import (
    PRODUCTION_TYPE,
    WORKING_SCHEMA_VERSION,
    new_working_state,
    reduce_state,
    revision_payload,
)


@dataclass(frozen=True, slots=True)
class CommandResult:
    classification: str
    receipt_id: str | None
    status: str | None
    current_state_version: int
    affected: dict[str, Any]
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class RevisionResult:
    classification: str
    receipt_id: str
    revision_id: str
    sequence: int
    content_hash: str
    state_version_at_capture: int


class ProductionService:
    def __init__(self, store: KernelStore, clock: Clock, ids: IdSource) -> None:
        self.store = store
        self.clock = clock
        self.ids = ids

    def create_project(self, name: str, creation_key: str | None = None) -> str:
        now = self.clock.now()
        with self.store.write() as db:
            if creation_key:
                existing = db.execute("SELECT id FROM projects WHERE creation_key = ?", (creation_key,)).fetchone()
                if existing:
                    return str(existing["id"])
            project_id = self.ids.new("prj")
            db.execute(
                "INSERT INTO projects(id,name,creation_key,created_at,updated_at) VALUES (?,?,?,?,?)",
                (project_id, name, creation_key, now, now),
            )
            return project_id

    def create_production(
        self,
        project_id: str,
        title: str,
        creation_key: str | None = None,
        production_type: str = PRODUCTION_TYPE,
    ) -> str:
        now = self.clock.now()
        if production_type != PRODUCTION_TYPE:
            raise InvalidCommand(f"unsupported production type: {production_type}")
        initial_state = new_working_state()
        with self.store.write() as db:
            if not db.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone():
                raise NotFound(f"project not found: {project_id}")
            if creation_key:
                existing = db.execute(
                    "SELECT id FROM productions WHERE project_id = ? AND creation_key = ?",
                    (project_id, creation_key),
                ).fetchone()
                if existing:
                    return str(existing["id"])
            production_id = self.ids.new("prod")
            db.execute(
                """
                INSERT INTO productions(
                    id,project_id,title,production_type,state_version,latest_captured_revision_id,status,creation_key,created_at,updated_at
                ) VALUES (?,?,?,?,0,NULL,'ACTIVE',?,?,?)
                """,
                (production_id, project_id, title, production_type, creation_key, now, now),
            )
            db.execute(
                "INSERT INTO working_states(production_id,schema_version,state_version,state_json,updated_at) VALUES (?,?,?,?,?)",
                (production_id, WORKING_SCHEMA_VERSION, 0, canonical_text(initial_state), now),
            )
            self._journal(db, production_id, "production", production_id, "PRODUCTION_CREATED", {"state_version": 0})
            return production_id

    def working_state(self, production_id: str) -> dict[str, Any]:
        with self.store.read() as db:
            row = db.execute(
                """
                SELECT p.state_version, p.latest_captured_revision_id, w.schema_version, w.state_json
                FROM productions p JOIN working_states w ON w.production_id = p.id
                WHERE p.id = ?
                """,
                (production_id,),
            ).fetchone()
            if not row:
                raise NotFound(f"production not found: {production_id}")
            return {
                "production_id": production_id,
                "state_version": int(row["state_version"]),
                "latest_captured_revision_id": row["latest_captured_revision_id"],
                "schema_version": int(row["schema_version"]),
                "state": json.loads(row["state_json"]),
            }

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
                current_version = self._current_version(db, production_id)
                if prior["command_fingerprint"] != fingerprint:
                    return CommandResult(
                        "IDEMPOTENCY_CONFLICT",
                        str(prior["id"]),
                        str(prior["status"]),
                        current_version,
                        json.loads(prior["affected_json"]),
                        "IDEMPOTENCY_CONFLICT",
                        "idempotency key was already used for a different command",
                    )
                return CommandResult(
                    "REPLAYED",
                    str(prior["id"]),
                    str(prior["status"]),
                    current_version,
                    json.loads(prior["affected_json"]),
                    prior["error_code"],
                    prior["error_message"],
                )

            row = db.execute(
                """
                SELECT p.state_version AS production_version, w.state_version AS working_version, w.state_json
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
                self._journal(db, production_id, "command_receipt", receipt_id, "COMMAND_REJECTED_STALE", {"current_state_version": current_version})
                return CommandResult("STALE_COMMAND", receipt_id, "REJECTED_STALE", current_version, {}, "STALE_COMMAND", f"current state version is {current_version}")

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
                "UPDATE working_states SET state_version = ?, state_json = ?, updated_at = ? WHERE production_id = ?",
                (new_version, canonical_text(new_state), now, production_id),
            )
            db.execute(
                "UPDATE productions SET state_version = ?, updated_at = ? WHERE id = ?",
                (new_version, now, production_id),
            )
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

    def capture_revision(
        self,
        *,
        principal: Principal,
        production_id: str,
        expected_state_version: int,
        idempotency_key: str,
    ) -> RevisionResult | CommandResult:
        action = "capture_revision"
        command_input: dict[str, Any] = {}
        fingerprint = self._command_fingerprint(principal, action, command_input)
        now = self.clock.now()
        with self.store.write() as db:
            prior = db.execute(
                "SELECT * FROM production_command_receipts WHERE production_id = ? AND idempotency_key = ?",
                (production_id, idempotency_key),
            ).fetchone()
            if prior:
                current_version = self._current_version(db, production_id)
                if prior["command_fingerprint"] != fingerprint:
                    return CommandResult("IDEMPOTENCY_CONFLICT", str(prior["id"]), str(prior["status"]), current_version, json.loads(prior["affected_json"]), "IDEMPOTENCY_CONFLICT", "idempotency key was already used for a different command")
                affected = json.loads(prior["affected_json"])
                if prior["status"] == "COMMITTED" and "revision_id" in affected:
                    revision = db.execute("SELECT * FROM production_revisions WHERE id = ?", (affected["revision_id"],)).fetchone()
                    return RevisionResult("REPLAYED", str(prior["id"]), str(revision["id"]), int(revision["sequence"]), str(revision["content_hash"]), int(revision["state_version_at_capture"]))
                return CommandResult("REPLAYED", str(prior["id"]), str(prior["status"]), current_version, affected, prior["error_code"], prior["error_message"])

            row = db.execute(
                """
                SELECT p.state_version, p.latest_captured_revision_id, p.production_type, w.schema_version, w.state_json
                FROM productions p JOIN working_states w ON w.production_id = p.id
                WHERE p.id = ?
                """,
                (production_id,),
            ).fetchone()
            if not row:
                raise NotFound(f"production not found: {production_id}")
            current_version = int(row["state_version"])
            if current_version != expected_state_version:
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
                self._journal(db, production_id, "command_receipt", receipt_id, "REVISION_CAPTURE_REJECTED_STALE", {"current_state_version": current_version})
                return CommandResult("STALE_COMMAND", receipt_id, "REJECTED_STALE", current_version, {}, "STALE_COMMAND", f"current state version is {current_version}")

            state = json.loads(row["state_json"])
            payload = revision_payload(state, str(row["production_type"]))
            canonical_json = canonical_text(payload)
            content_hash = canonical_hash(payload)
            head_id = row["latest_captured_revision_id"]
            if head_id:
                head = db.execute("SELECT * FROM production_revisions WHERE id = ?", (head_id,)).fetchone()
                if head and head["content_hash"] == content_hash:
                    affected = {"revision_id": str(head["id"]), "capture_kind": "NO_CHANGE"}
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
                        resulting_state_version=current_version,
                        affected=affected,
                        error_code=None,
                        error_message=None,
                        now=now,
                    )
                    self._journal(db, production_id, "production_revision", str(head["id"]), "PRODUCTION_REVISION_CAPTURE_REPLAYED", affected)
                    return RevisionResult("NO_CHANGE", receipt_id, str(head["id"]), int(head["sequence"]), str(head["content_hash"]), int(head["state_version_at_capture"]))

            sequence = int(db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM production_revisions WHERE production_id = ?", (production_id,)).fetchone()[0])
            revision_id = self.ids.new("rev")
            db.execute(
                """
                INSERT INTO production_revisions(
                    id,production_id,sequence,parent_revision_id,state_version_at_capture,schema_version,canonical_json,content_hash,captured_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (revision_id, production_id, sequence, head_id, current_version, int(row["schema_version"]), canonical_json, content_hash, now),
            )
            db.execute("UPDATE productions SET latest_captured_revision_id = ?, updated_at = ? WHERE id = ?", (revision_id, now, production_id))
            affected = {"revision_id": revision_id, "capture_kind": "NEW"}
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
                resulting_state_version=current_version,
                affected=affected,
                error_code=None,
                error_message=None,
                now=now,
            )
            self._journal(db, production_id, "production_revision", revision_id, "PRODUCTION_REVISION_CAPTURED", {"sequence": sequence, "content_hash": content_hash})
            return RevisionResult("CAPTURED", receipt_id, revision_id, sequence, content_hash, current_version)

    def revision(self, revision_id: str) -> dict[str, Any]:
        with self.store.read() as db:
            row = db.execute("SELECT * FROM production_revisions WHERE id = ?", (revision_id,)).fetchone()
            if not row:
                raise NotFound(f"revision not found: {revision_id}")
            return dict(row)

    def revisions(self, production_id: str) -> list[dict[str, Any]]:
        with self.store.read() as db:
            return [dict(row) for row in db.execute("SELECT * FROM production_revisions WHERE production_id = ? ORDER BY sequence", (production_id,))]

    def _command_fingerprint(self, principal: Principal, action: str, command_input: dict[str, Any]) -> str:
        return canonical_hash({"principal_type": principal.type.value, "principal_id": principal.id, "action": action, "input": command_input})

    def _current_version(self, db: Any, production_id: str) -> int:
        row = db.execute("SELECT state_version FROM productions WHERE id = ?", (production_id,)).fetchone()
        if not row:
            raise NotFound(f"production not found: {production_id}")
        return int(row["state_version"])

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
