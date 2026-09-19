from __future__ import annotations

import json
from typing import Any

from solostudio.kernel.artifacts import PreparedArtifact
from solostudio.kernel.errors import NotFound
from solostudio.kernel.identity import canonical_hash, canonical_text
from solostudio.kernel.principals import Principal
from solostudio.kernel.productions.models import CommandResult, RevisionResult
from solostudio.kernel.productions.state import revision_payload


class RevisionMixin:
    def capture_revision(
        self,
        *,
        principal: Principal,
        production_id: str,
        expected_state_version: int,
        idempotency_key: str,
    ) -> RevisionResult | CommandResult:
        action = "capture_revision"
        fingerprint = self._command_fingerprint(principal, action, {})
        now = self.clock.now()

        with self.store.read() as db:
            prior = db.execute(
                "SELECT * FROM production_command_receipts WHERE production_id = ? AND idempotency_key = ?",
                (production_id, idempotency_key),
            ).fetchone()
            if prior:
                return self._capture_from_prior(db, prior, fingerprint, production_id)
            snapshot = db.execute(
                """
                SELECT p.state_version,p.latest_captured_revision_id,p.production_type,
                       w.state_version AS working_version,w.schema_version,w.state_json
                FROM productions p JOIN working_states w ON w.production_id = p.id
                WHERE p.id = ?
                """,
                (production_id,),
            ).fetchone()
            if not snapshot:
                raise NotFound(f"production not found: {production_id}")
            if snapshot["state_version"] != snapshot["working_version"]:
                raise RuntimeError("working state version invariant violated")
            snapshot_version = int(snapshot["state_version"])
            snapshot_json = str(snapshot["state_json"])
            snapshot_schema_version = int(snapshot["schema_version"])
            production_type = str(snapshot["production_type"])

        if snapshot_version != expected_state_version:
            with self.store.write() as db:
                prior = db.execute(
                    "SELECT * FROM production_command_receipts WHERE production_id = ? AND idempotency_key = ?",
                    (production_id, idempotency_key),
                ).fetchone()
                if prior:
                    return self._capture_from_prior(db, prior, fingerprint, production_id)
                current_version = self._current_version(db, production_id)
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
                    "REVISION_CAPTURE_REJECTED_STALE",
                )

        state = json.loads(snapshot_json)
        payload = revision_payload(state, production_type)
        canonical_json = canonical_text(payload)
        content_hash = canonical_hash(payload)
        prepared_sources = self._prepare_capture_sources(state)

        with self.store.write() as db:
            prior = db.execute(
                "SELECT * FROM production_command_receipts WHERE production_id = ? AND idempotency_key = ?",
                (production_id, idempotency_key),
            ).fetchone()
            if prior:
                return self._capture_from_prior(db, prior, fingerprint, production_id)
            row = db.execute(
                """
                SELECT p.state_version,p.latest_captured_revision_id,w.state_version AS working_version,w.state_json
                FROM productions p JOIN working_states w ON w.production_id = p.id
                WHERE p.id = ?
                """,
                (production_id,),
            ).fetchone()
            if not row:
                raise NotFound(f"production not found: {production_id}")
            if row["state_version"] != row["working_version"]:
                raise RuntimeError("working state version invariant violated")
            current_version = int(row["state_version"])
            if current_version != expected_state_version or str(row["state_json"]) != snapshot_json:
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
                    "REVISION_CAPTURE_REJECTED_STALE",
                )

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
                (revision_id, production_id, sequence, head_id, current_version, snapshot_schema_version, canonical_json, content_hash, now),
            )
            artifact_ids: list[str] = []
            for prepared in prepared_sources:
                artifact_ids.append(
                    self.artifacts.register_prepared_in_tx(
                        db,
                        prepared,
                        production_id=production_id,
                        production_revision_id=revision_id,
                    )
                )
            db.execute("UPDATE productions SET latest_captured_revision_id = ?,updated_at = ? WHERE id = ?", (revision_id, now, production_id))
            affected = {"revision_id": revision_id, "capture_kind": "NEW", "artifact_ids": artifact_ids}
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
            self._journal(
                db,
                production_id,
                "production_revision",
                revision_id,
                "PRODUCTION_REVISION_CAPTURED",
                {"sequence": sequence, "content_hash": content_hash, "artifact_ids": artifact_ids},
            )
            return RevisionResult("CAPTURED", receipt_id, revision_id, sequence, content_hash, current_version)

    def _prepare_capture_sources(self, state: dict[str, Any]) -> list[PreparedArtifact]:
        prepared: list[PreparedArtifact] = []
        script = state.get("script", {}).get("text", "")
        if script:
            prepared.append(
                self.artifacts.prepare_bytes(
                    script.encode("utf-8"),
                    kind="script_text",
                    media_type="text/plain; charset=utf-8",
                    producer_stage="revision_capture",
                    metadata={"source": "working_state.script.text"},
                )
            )
        visual_plan = state.get("visual_plan", [])
        if visual_plan:
            prepared.append(
                self.artifacts.prepare_bytes(
                    canonical_text(visual_plan).encode("utf-8"),
                    kind="visual_plan",
                    media_type="application/json",
                    producer_stage="revision_capture",
                    metadata={"source": "working_state.visual_plan"},
                )
            )
        return prepared
