from __future__ import annotations

from typing import Any


class RecoveryMixin:
    def recover_startup(self) -> int:
        now = self.clock.now()
        recovered = 0
        with self.store.write() as db:
            rows = list(db.execute(
                """
                SELECT a.*,j.production_id,j.max_attempts
                FROM attempts a JOIN job_specs j ON j.id=a.job_id
                WHERE a.state='RUNNING'
                ORDER BY a.id
                """
            ))
            for row in rows:
                recovered += 1
                db.execute(
                    "UPDATE attempts SET state='INTERRUPTED',finished_at=?,error_code='KERNEL_RESTART',error_message='kernel restarted while attempt was running' WHERE id=?",
                    (now, row["id"]),
                )
                next_state = self._schedule_interrupted_retry_or_fail(db, row, now)
                self._journal(db, str(row["production_id"]), "attempt", str(row["id"]), "ATTEMPT_INTERRUPTED", {
                    "job_state": next_state,
                    "reason": "KERNEL_RESTART",
                })
        return recovered

    def _schedule_interrupted_retry_or_fail(self, db: Any, row: Any, now: str) -> str:
        job_id = str(row["job_id"] if "job_id" in row.keys() else row["id"])
        attempt_number = int(row["attempt_number"])
        max_attempts = int(row["max_attempts"]) if "max_attempts" in row.keys() else int(
            db.execute("SELECT max_attempts FROM job_specs WHERE id=?", (job_id,)).fetchone()[0]
        )
        if attempt_number < max_attempts:
            next_attempt_id = self.ids.new("attempt")
            self._insert_attempt(db, job_id, next_attempt_id, attempt_number + 1)
            db.execute("UPDATE job_specs SET state='QUEUED',finished_at=NULL WHERE id=?", (job_id,))
            return "QUEUED"
        db.execute("UPDATE job_specs SET state='FAILED',finished_at=? WHERE id=?", (now, job_id))
        return "FAILED"

