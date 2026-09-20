from __future__ import annotations

from typing import Any

from solostudio.kernel.clock import Clock
from solostudio.kernel.costs.models import CostPlan
from solostudio.kernel.errors import InvalidCostState, NotFound
from solostudio.kernel.identity import canonical_text
from solostudio.kernel.ids import IdSource
from solostudio.kernel.store import KernelStore


class CostService:
    def __init__(self, store: KernelStore, clock: Clock, ids: IdSource) -> None:
        self.store = store
        self.clock = clock
        self.ids = ids

    def reserve_unbound_in_tx(self, db: Any, production_id: str, plan: CostPlan) -> str:
        cost_id = self.ids.new("cost")
        now = self.clock.now()
        db.execute(
            """
            INSERT INTO cost_ledger(
                id,production_id,job_id,capability,state,estimated_microunits,
                reserved_microunits,settled_microunits,unit,created_at,settled_at
            ) VALUES (?,?,NULL,?,'ESTIMATED',?,?,NULL,?,?,NULL)
            """,
            (
                cost_id,
                production_id,
                plan.capability,
                plan.estimated_microunits,
                0,
                plan.unit,
                now,
            ),
        )
        self._journal(db, production_id, cost_id, "COST_ESTIMATED", {
            "capability": plan.capability,
            "estimated_microunits": plan.estimated_microunits,
        })
        db.execute(
            "UPDATE cost_ledger SET state='RESERVED',reserved_microunits=? WHERE id=?",
            (plan.reserved_microunits, cost_id),
        )
        self._journal(db, production_id, cost_id, "COST_RESERVED", {
            "reserved_microunits": plan.reserved_microunits,
        })
        return cost_id

    def bind_job_in_tx(self, db: Any, cost_id: str, job_id: str) -> None:
        row = db.execute("SELECT state,job_id FROM cost_ledger WHERE id=?", (cost_id,)).fetchone()
        if not row:
            raise NotFound(f"cost record not found: {cost_id}")
        if row["state"] != "RESERVED" or row["job_id"] is not None:
            raise InvalidCostState("only an unbound reservation can bind a job")
        db.execute("UPDATE cost_ledger SET job_id=? WHERE id=?", (job_id, cost_id))

    def settle_job_in_tx(self, db: Any, job_id: str, settled_microunits: int | None = None) -> str | None:
        row = self._active_for_job(db, job_id)
        if not row:
            return None
        if row["state"] != "RESERVED":
            raise InvalidCostState("only a reserved cost can settle")
        settled = int(row["reserved_microunits"]) if settled_microunits is None else settled_microunits
        if type(settled) is not int:
            raise InvalidCostState("settled cost must be integer microunits")
        if settled < 0:
            raise InvalidCostState("settled cost must be non-negative")
        now = self.clock.now()
        db.execute(
            "UPDATE cost_ledger SET state='SETTLED',settled_microunits=?,settled_at=? WHERE id=?",
            (settled, now, row["id"]),
        )
        self._journal(db, str(row["production_id"]), str(row["id"]), "COST_SETTLED", {
            "settled_microunits": settled,
        })
        return str(row["id"])

    def release_job_in_tx(self, db: Any, job_id: str) -> str | None:
        row = self._active_for_job(db, job_id)
        if not row:
            return None
        if row["state"] != "RESERVED":
            raise InvalidCostState("only a reserved cost can release")
        now = self.clock.now()
        db.execute(
            "UPDATE cost_ledger SET state='RELEASED',settled_microunits=0,settled_at=? WHERE id=?",
            (now, row["id"]),
        )
        self._journal(db, str(row["production_id"]), str(row["id"]), "COST_RELEASED", {})
        return str(row["id"])

    def mark_unknown_job_in_tx(self, db: Any, job_id: str) -> str | None:
        row = self._active_for_job(db, job_id)
        if not row:
            return None
        if row["state"] != "RESERVED":
            raise InvalidCostState("only a reserved cost can become unknown")
        db.execute("UPDATE cost_ledger SET state='UNKNOWN' WHERE id=?", (row["id"],))
        self._journal(db, str(row["production_id"]), str(row["id"]), "COST_UNKNOWN", {})
        return str(row["id"])

    def for_job(self, job_id: str) -> dict[str, Any] | None:
        with self.store.read() as db:
            row = db.execute(
                "SELECT * FROM cost_ledger WHERE job_id=? ORDER BY created_at,id LIMIT 1",
                (job_id,),
            ).fetchone()
            return dict(row) if row else None

    def recover_unbound_reservations(self) -> int:
        now = self.clock.now()
        with self.store.write() as db:
            rows = list(db.execute("SELECT * FROM cost_ledger WHERE state='RESERVED' AND job_id IS NULL"))
            for row in rows:
                db.execute(
                    "UPDATE cost_ledger SET state='RELEASED',settled_microunits=0,settled_at=? WHERE id=?",
                    (now, row["id"]),
                )
                self._journal(db, str(row["production_id"]), str(row["id"]), "COST_RELEASED", {
                    "reason": "UNBOUND_STARTUP_RECOVERY",
                })
            return len(rows)

    @staticmethod
    def _active_for_job(db: Any, job_id: str):
        return db.execute(
            "SELECT * FROM cost_ledger WHERE job_id=? AND state IN ('RESERVED','UNKNOWN') ORDER BY created_at,id LIMIT 1",
            (job_id,),
        ).fetchone()

    def _journal(self, db: Any, production_id: str, cost_id: str, event_type: str, event: dict[str, Any]) -> None:
        db.execute(
            "INSERT INTO journal_entries(production_id,entity_type,entity_id,event_type,event_json,created_at) VALUES (?,?,?,?,?,?)",
            (production_id, "cost_ledger", cost_id, event_type, canonical_text(event), self.clock.now()),
        )
