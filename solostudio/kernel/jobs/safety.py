from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from solostudio.kernel.errors import InvalidArtifact, InvalidCommand, NotFound


class SafeExecutionMixin:
    """Crash-recoverable attempt start and authoritative artifact completion fencing."""

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
            if path.exists() or path.is_symlink():
                if path.is_symlink() or not path.is_dir():
                    raise RuntimeError(f"attempt temp namespace is not a directory: {row['temp_relpath']}")
                shutil.rmtree(path)
            path.mkdir(parents=True, exist_ok=False)

            db.execute(
                "UPDATE attempts SET state='RUNNING',executor_identity=?,started_at=? WHERE id=?",
                (executor_identity, now, attempt_id),
            )
            db.execute("UPDATE job_specs SET state='RUNNING' WHERE id=?", (row["job_id"],))
            self._journal(
                db,
                str(row["production_id"]),
                "attempt",
                attempt_id,
                "ATTEMPT_STARTED",
                {
                    "job_id": str(row["job_id"]),
                    "temp_relpath": str(row["temp_relpath"]),
                    "executor_identity": executor_identity,
                },
            )
            return path

    def complete_artifact_attempt(self, attempt_id: str, outputs: list[dict[str, Any]]) -> list[str]:
        if not isinstance(outputs, list):
            raise InvalidArtifact("artifact worker outputs must be a list")
        if outputs:
            for output in outputs:
                if not isinstance(output, dict):
                    raise InvalidArtifact("artifact worker outputs must be objects")

        attempt = self.attempt(attempt_id)
        job = self.job(str(attempt["job_id"]))
        authoritative_fingerprint = str(job["input_fingerprint"])
        normalized = []
        for output in outputs:
            item = dict(output)
            item["input_fingerprint"] = authoritative_fingerprint
            normalized.append(item)
        return super().complete_artifact_attempt(attempt_id, normalized)
