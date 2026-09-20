from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from solostudio.kernel.errors import InvalidArtifact, InvalidCommand, NotFound
from solostudio.kernel.identity import canonical_text
from solostudio.kernel.principals import Principal


class ExecutionMixin:
    def start_attempt(self, attempt_id: str, executor_identity: str) -> Path:
        now = self.clock.now()
        with self.store.write() as db:
            row = db.execute(
                """
                SELECT a.*,j.production_id,j.state AS job_state
                FROM attempts a JOIN job_specs j ON j.id = a.job_id
                WHERE a.id = ?
                """,
                (attempt_id,),
            ).fetchone()
            if not row:
                raise NotFound(f"attempt not found: {attempt_id}")
            if row["state"] != "CREATED" or row["job_state"] != "QUEUED":
                raise InvalidCommand("attempt can start only from CREATED under QUEUED job")
            path = self.data_dir / str(row["temp_relpath"])
            if path.exists():
                raise RuntimeError(f"attempt temp namespace already exists: {row['temp_relpath']}")
            path.mkdir(parents=True, exist_ok=False)
            db.execute(
                "UPDATE attempts SET state='RUNNING',executor_identity=?,started_at=? WHERE id=?",
                (executor_identity, now, attempt_id),
            )
            db.execute("UPDATE job_specs SET state='RUNNING' WHERE id=?", (row["job_id"],))
            self._journal(db, str(row["production_id"]), "attempt", attempt_id, "ATTEMPT_STARTED", {
                "job_id": str(row["job_id"]),
                "temp_relpath": str(row["temp_relpath"]),
                "executor_identity": executor_identity,
            })
            return path

    def request_payload(self, attempt_id: str) -> dict[str, Any]:
        with self.store.read() as db:
            row = db.execute(
                """
                SELECT a.id AS attempt_id,a.attempt_number,a.temp_relpath,
                       j.*
                FROM attempts a JOIN job_specs j ON j.id = a.job_id
                WHERE a.id = ?
                """,
                (attempt_id,),
            ).fetchone()
            if not row:
                raise NotFound(f"attempt not found: {attempt_id}")
            return {
                "schema_version": 1,
                "job_id": str(row["id"]),
                "attempt_id": str(row["attempt_id"]),
                "attempt_number": int(row["attempt_number"]),
                "job_class": str(row["job_class"]),
                "job_type": str(row["job_type"]),
                "semantic_capability": str(row["semantic_capability"]),
                "source_state_version": row["source_state_version"],
                "production_revision_id": row["production_revision_id"],
                "variant_id": row["variant_id"],
                "input_fingerprint": str(row["input_fingerprint"]),
                "spec": json.loads(row["spec_json"]),
                "route": json.loads(row["route_json"]),
            }

    def complete_state_proposal(self, attempt_id: str, proposal: dict[str, Any]) -> None:
        action = proposal.get("action")
        command_input = proposal.get("input")
        if not isinstance(action, str) or not isinstance(command_input, dict):
            raise InvalidCommand("state proposal requires action and object input")
        now = self.clock.now()
        with self.store.write() as db:
            row = self._running_attempt_row(db, attempt_id)
            if row["job_class"] != "STATE_PROPOSAL":
                raise InvalidCommand("attempt is not a state proposal job")
            result = {"proposal": {"action": action, "input": command_input}}
            db.execute(
                "UPDATE attempts SET state='SUCCEEDED',result_json=?,finished_at=? WHERE id=?",
                (canonical_text(result), now, attempt_id),
            )
            db.execute("UPDATE job_specs SET state='SUCCEEDED',finished_at=? WHERE id=?", (now, row["job_id"]))
            self.costs.settle_job_in_tx(db, str(row["job_id"]))
            self._journal(db, str(row["production_id"]), "attempt", attempt_id, "ATTEMPT_SUCCEEDED", result)

    def complete_artifact_attempt(self, attempt_id: str, outputs: list[dict[str, Any]]) -> list[str]:
        if not outputs:
            raise InvalidArtifact("artifact worker returned no outputs")
        with self.store.read() as db:
            row = db.execute(
                """
                SELECT a.*,j.production_id,j.job_class,j.production_revision_id,j.variant_id,j.state AS job_state
                FROM attempts a JOIN job_specs j ON j.id = a.job_id WHERE a.id = ?
                """,
                (attempt_id,),
            ).fetchone()
            if not row:
                raise NotFound(f"attempt not found: {attempt_id}")
            if row["state"] != "RUNNING" or row["job_state"] != "RUNNING":
                raise InvalidCommand("artifact completion requires a running attempt")
            if row["job_class"] != "ARTIFACT":
                raise InvalidCommand("attempt is not an artifact job")
            temp_root = (self.data_dir / str(row["temp_relpath"])).resolve()
            prepared = []
            for output in outputs:
                rel = output.get("path")
                if not isinstance(rel, str) or not rel:
                    raise InvalidArtifact("worker output path is required")
                path = (temp_root / rel).resolve()
                if temp_root not in path.parents:
                    raise InvalidArtifact("worker output path escapes attempt namespace")
                if not path.is_file():
                    raise InvalidArtifact(f"expected worker output is missing: {rel}")
                extra_metadata = output.get("metadata", {})
                if not isinstance(extra_metadata, dict):
                    raise InvalidArtifact("worker output metadata must be an object")
                metadata = dict(extra_metadata)
                metadata["worker_output_role"] = output.get("role", "primary")
                prepared.append((output, self.artifacts.prepare_bytes(
                    path.read_bytes(),
                    kind=str(output.get("kind", "source_reference")),
                    media_type=str(output.get("media_type", "application/octet-stream")),
                    producer_stage=str(output.get("producer_stage", "media_worker")),
                    input_fingerprint=output.get("input_fingerprint"),
                    metadata=metadata,
                )))

        now = self.clock.now()
        with self.store.write() as db:
            row = self._running_attempt_row(db, attempt_id)
            if row["job_class"] != "ARTIFACT":
                raise InvalidCommand("attempt is not an artifact job")
            artifact_ids: list[str] = []
            rendered_video_registered = False
            for _output, prepared_artifact in prepared:
                artifact_ids.append(self.artifacts.register_prepared_in_tx(
                    db,
                    prepared_artifact,
                    production_id=str(row["production_id"]),
                    production_revision_id=row["production_revision_id"],
                    variant_id=row["variant_id"],
                    producer_job_id=str(row["job_id"]),
                    producer_attempt_id=attempt_id,
                ))
                rendered_video_registered = rendered_video_registered or prepared_artifact.kind == "rendered_video"
            result = {"artifact_ids": artifact_ids}
            db.execute(
                "UPDATE attempts SET state='SUCCEEDED',result_json=?,finished_at=? WHERE id=?",
                (canonical_text(result), now, attempt_id),
            )
            db.execute("UPDATE job_specs SET state='SUCCEEDED',finished_at=? WHERE id=?", (now, row["job_id"]))
            if rendered_video_registered and row["variant_id"] is not None:
                if row["job_type"] != "MEDIA_RENDER" or row["semantic_capability"] != "media.render":
                    raise InvalidCommand("only an authoritative media.render JobSpec can make a variant ready")
                updated = db.execute(
                    "UPDATE delivery_variants SET state='READY' WHERE id=? AND state IN ('PROPOSED','READY')",
                    (row["variant_id"],),
                ).rowcount
                if updated != 1:
                    raise InvalidCommand("rendered video cannot make the bound variant ready")
                self._journal(
                    db,
                    str(row["production_id"]),
                    "delivery_variant",
                    str(row["variant_id"]),
                    "DELIVERY_VARIANT_READY",
                    {"render_artifact_ids": artifact_ids},
                )
            self.costs.settle_job_in_tx(db, str(row["job_id"]))
            self._journal(db, str(row["production_id"]), "attempt", attempt_id, "ATTEMPT_SUCCEEDED", result)
            return artifact_ids

    def fail_attempt(
        self,
        attempt_id: str,
        error_code: str,
        error_message: str,
        *,
        billing_ambiguous: bool = False,
    ) -> str:
        now = self.clock.now()
        with self.store.write() as db:
            row = self._running_attempt_row(db, attempt_id)
            db.execute(
                "UPDATE attempts SET state='FAILED',finished_at=?,error_code=?,error_message=? WHERE id=?",
                (now, error_code, error_message, attempt_id),
            )
            db.execute("UPDATE job_specs SET state='FAILED',finished_at=? WHERE id=?", (now, row["job_id"]))
            if billing_ambiguous:
                self.costs.mark_unknown_job_in_tx(db, str(row["job_id"]))
            else:
                self.costs.release_job_in_tx(db, str(row["job_id"]))
            self._journal(db, str(row["production_id"]), "attempt", attempt_id, "ATTEMPT_FAILED", {
                "error_code": error_code,
                "job_state": "FAILED",
                "billing_ambiguous": billing_ambiguous,
            })
            return "FAILED"

    def adopt_state_proposal(self, job_id: str, principal: Principal, idempotency_key: str):
        with self.store.read() as db:
            job = db.execute("SELECT * FROM job_specs WHERE id = ?", (job_id,)).fetchone()
            if not job:
                raise NotFound(f"job not found: {job_id}")
            if job["job_class"] != "STATE_PROPOSAL" or job["state"] != "SUCCEEDED":
                raise InvalidCommand("job does not contain a successful state proposal")
            attempt = db.execute(
                "SELECT result_json FROM attempts WHERE job_id = ? AND state='SUCCEEDED' ORDER BY attempt_number DESC LIMIT 1",
                (job_id,),
            ).fetchone()
            if not attempt or not attempt["result_json"]:
                raise RuntimeError("successful state job has no proposal result")
            proposal = json.loads(attempt["result_json"])["proposal"]
            source_state_version = int(job["source_state_version"])
            production_id = str(job["production_id"])
        return self.productions.handle_command(
            principal=principal,
            production_id=production_id,
            expected_state_version=source_state_version,
            idempotency_key=idempotency_key,
            action=str(proposal["action"]),
            command_input=dict(proposal["input"]),
        )

    def _running_attempt_row(self, db: Any, attempt_id: str):
        row = db.execute(
            """
            SELECT a.*,j.production_id,j.job_class,j.job_type,j.semantic_capability,
                   j.production_revision_id,j.variant_id,j.state AS job_state,j.max_attempts
            FROM attempts a JOIN job_specs j ON j.id=a.job_id WHERE a.id=?
            """,
            (attempt_id,),
        ).fetchone()
        if not row:
            raise NotFound(f"attempt not found: {attempt_id}")
        if row["state"] != "RUNNING" or row["job_state"] != "RUNNING":
            raise InvalidCommand("attempt is not running")
        return row
