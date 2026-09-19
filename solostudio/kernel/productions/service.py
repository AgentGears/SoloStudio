from __future__ import annotations

import json
from typing import Any

from solostudio.kernel.artifacts import ArtifactService
from solostudio.kernel.clock import Clock
from solostudio.kernel.errors import InvalidCommand, NotFound
from solostudio.kernel.identity import canonical_text
from solostudio.kernel.ids import IdSource
from solostudio.kernel.store import KernelStore
from solostudio.kernel.productions.commands import CommandMixin
from solostudio.kernel.productions.models import CommandResult, RevisionResult
from solostudio.kernel.productions.receipts import ReceiptMixin
from solostudio.kernel.productions.revisions import RevisionMixin
from solostudio.kernel.productions.state import PRODUCTION_TYPE, WORKING_SCHEMA_VERSION, new_working_state


class ProductionService(CommandMixin, RevisionMixin, ReceiptMixin):
    def __init__(self, store: KernelStore, clock: Clock, ids: IdSource, artifacts: ArtifactService) -> None:
        self.store = store
        self.clock = clock
        self.ids = ids
        self.artifacts = artifacts

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
                SELECT p.state_version,p.latest_captured_revision_id,w.schema_version,w.state_json
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

    def revision(self, revision_id: str) -> dict[str, Any]:
        with self.store.read() as db:
            row = db.execute("SELECT * FROM production_revisions WHERE id = ?", (revision_id,)).fetchone()
            if not row:
                raise NotFound(f"revision not found: {revision_id}")
            return dict(row)

    def revisions(self, production_id: str) -> list[dict[str, Any]]:
        with self.store.read() as db:
            return [dict(row) for row in db.execute("SELECT * FROM production_revisions WHERE production_id = ? ORDER BY sequence", (production_id,))]

