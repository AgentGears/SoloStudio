from __future__ import annotations

import json
from typing import Any

from solostudio.kernel.costs import CostPlan
from solostudio.kernel.errors import InvalidCommand, NotFound
from solostudio.kernel.identity import canonical_hash, canonical_text
from solostudio.kernel.jobs.models import JobAdmission


JOB_CLASSES = {"STATE_PROPOSAL", "ARTIFACT"}


class AdmissionMixin:
    def admit(
        self,
        *,
        production_id: str,
        job_class: str,
        job_type: str,
        semantic_capability: str,
        spec: dict[str, Any],
        route: dict[str, Any],
        input_fingerprint: str,
        production_revision_id: str | None = None,
        variant_id: str | None = None,
        max_attempts: int = 2,
        cost_plan: CostPlan | None = None,
        expected_source_state_version: int | None = None,
    ) -> JobAdmission:
        if job_class not in JOB_CLASSES:
            raise InvalidCommand(f"unsupported job class: {job_class}")
        if type(max_attempts) is not int or max_attempts < 1:
            raise InvalidCommand("max_attempts must be a positive integer")
        if not job_type or not semantic_capability or not input_fingerprint:
            raise InvalidCommand("job type, capability, and input fingerprint are required")
        if cost_plan is not None and cost_plan.capability != semantic_capability:
            raise InvalidCommand("cost plan capability must match job capability")
        now = self.clock.now()
        spec_json = canonical_text(spec)
        route_json = canonical_text(route)
        spec_hash = canonical_hash({
            "job_class": job_class,
            "job_type": job_type,
            "semantic_capability": semantic_capability,
            "spec": spec,
            "route": route,
            "input_fingerprint": input_fingerprint,
            "production_revision_id": production_revision_id,
            "variant_id": variant_id,
        })
        with self.store.write() as db:
            production = db.execute(
                "SELECT state_version FROM productions WHERE id = ?",
                (production_id,),
            ).fetchone()
            if not production:
                raise NotFound(f"production not found: {production_id}")
            source_state_version: int | None = None
            if job_class == "STATE_PROPOSAL":
                if production_revision_id is not None or variant_id is not None:
                    raise InvalidCommand("STATE_PROPOSAL jobs cannot bind captured revision or variant lineage in M0")
                source_state_version = int(production["state_version"])
                if expected_source_state_version is not None and source_state_version != expected_source_state_version:
                    raise InvalidCommand(
                        f"state changed during capability admission: expected {expected_source_state_version}, current is {source_state_version}"
                    )
            else:
                if production_revision_id is None:
                    raise InvalidCommand("ARTIFACT jobs require a captured production revision")
                revision = db.execute(
                    "SELECT production_id FROM production_revisions WHERE id = ?",
                    (production_revision_id,),
                ).fetchone()
                if not revision:
                    raise NotFound(f"revision not found: {production_revision_id}")
                if str(revision["production_id"]) != production_id:
                    raise InvalidCommand("artifact job revision belongs to another production")
                if variant_id is not None:
                    variant = db.execute(
                        "SELECT production_id,source_revision_id FROM delivery_variants WHERE id=?",
                        (variant_id,),
                    ).fetchone()
                    if not variant:
                        raise NotFound(f"variant not found: {variant_id}")
                    if str(variant["production_id"]) != production_id:
                        raise InvalidCommand("artifact job variant belongs to another production")
                    if str(variant["source_revision_id"]) != production_revision_id:
                        raise InvalidCommand("artifact job variant and captured revision lineage disagree")

            active_rows = list(db.execute(
                """
                SELECT id,route_json,max_attempts,variant_id FROM job_specs
                WHERE production_id = ? AND semantic_capability = ? AND input_fingerprint = ?
                  AND state IN ('QUEUED','RUNNING')
                ORDER BY created_at, id
                """,
                (production_id, semantic_capability, input_fingerprint),
            ))
            for active in active_rows:
                if str(active["route_json"]) != route_json:
                    continue
                if int(active["max_attempts"]) != max_attempts:
                    continue
                if variant_id is not None and active["variant_id"] != variant_id:
                    continue
                cost_row = db.execute(
                    """
                    SELECT id,state,capability,estimated_microunits,reserved_microunits,unit
                    FROM cost_ledger WHERE job_id=? ORDER BY created_at,id LIMIT 1
                    """,
                    (active["id"],),
                ).fetchone()
                if not self._cost_contract_matches(cost_row, cost_plan):
                    continue
                attempt = db.execute(
                    "SELECT id FROM attempts WHERE job_id = ? ORDER BY attempt_number DESC LIMIT 1",
                    (active["id"],),
                ).fetchone()
                if not attempt:
                    raise RuntimeError("active job has no attempt")
                return JobAdmission(
                    str(active["id"]),
                    str(attempt["id"]),
                    True,
                    str(cost_row["id"]) if cost_row else None,
                )

            job_id = self.ids.new("job")
            attempt_id = self.ids.new("attempt")
            cost_id = None
            if cost_plan is not None:
                cost_id = self.costs.reserve_unbound_in_tx(db, production_id, cost_plan)
            db.execute(
                """
                INSERT INTO job_specs(
                    id,production_id,job_class,source_state_version,production_revision_id,variant_id,
                    job_type,semantic_capability,spec_json,spec_hash,route_json,input_fingerprint,
                    state,max_attempts,created_at,finished_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'QUEUED',?,?,NULL)
                """,
                (
                    job_id, production_id, job_class, source_state_version, production_revision_id, variant_id,
                    job_type, semantic_capability, spec_json, spec_hash, route_json, input_fingerprint,
                    max_attempts, now,
                ),
            )
            if cost_id is not None:
                self.costs.bind_job_in_tx(db, cost_id, job_id)
            self._insert_attempt(db, job_id, attempt_id, 1)
            self._journal(db, production_id, "job_spec", job_id, "JOB_ADMITTED", {
                "job_class": job_class,
                "semantic_capability": semantic_capability,
                "input_fingerprint": input_fingerprint,
                "variant_id": variant_id,
            })
            return JobAdmission(job_id, attempt_id, False, cost_id)

    @staticmethod
    def _cost_contract_matches(cost_row: Any, cost_plan: CostPlan | None) -> bool:
        if cost_plan is None:
            return cost_row is None
        if cost_row is None or cost_row["state"] != "RESERVED":
            return False
        return (
            str(cost_row["capability"]) == cost_plan.capability
            and int(cost_row["estimated_microunits"]) == cost_plan.estimated_microunits
            and int(cost_row["reserved_microunits"]) == cost_plan.reserved_microunits
            and str(cost_row["unit"]) == cost_plan.unit
        )

    def job(self, job_id: str) -> dict[str, Any]:
        with self.store.read() as db:
            row = db.execute("SELECT * FROM job_specs WHERE id = ?", (job_id,)).fetchone()
            if not row:
                raise NotFound(f"job not found: {job_id}")
            result = dict(row)
        result["spec"] = json.loads(result["spec_json"])
        result["route"] = json.loads(result["route_json"])
        return result

    def attempt(self, attempt_id: str) -> dict[str, Any]:
        with self.store.read() as db:
            row = db.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
            if not row:
                raise NotFound(f"attempt not found: {attempt_id}")
            result = dict(row)
        result["progress"] = json.loads(result["progress_json"])
        result["result"] = json.loads(result["result_json"]) if result["result_json"] else None
        return result

    def attempts(self, job_id: str) -> list[dict[str, Any]]:
        with self.store.read() as db:
            ids = [str(row["id"]) for row in db.execute(
                "SELECT id FROM attempts WHERE job_id = ? ORDER BY attempt_number",
                (job_id,),
            )]
        return [self.attempt(attempt_id) for attempt_id in ids]

    def _insert_attempt(self, db: Any, job_id: str, attempt_id: str, attempt_number: int) -> None:
        temp_relpath = f"tmp/{job_id}/{attempt_id}"
        db.execute(
            """
            INSERT INTO attempts(
                id,job_id,attempt_number,state,temp_relpath,executor_identity,result_json,started_at,finished_at,
                error_code,error_message,progress_json
            ) VALUES (?,?,?,'CREATED',?,NULL,NULL,NULL,NULL,NULL,NULL,?)
            """,
            (attempt_id, job_id, attempt_number, temp_relpath, canonical_text({})),
        )

    def _journal(self, db: Any, production_id: str, entity_type: str, entity_id: str, event_type: str, event: dict[str, Any]) -> None:
        db.execute(
            "INSERT INTO journal_entries(production_id,entity_type,entity_id,event_type,event_json,created_at) VALUES (?,?,?,?,?,?)",
            (production_id, entity_type, entity_id, event_type, canonical_text(event), self.clock.now()),
        )
