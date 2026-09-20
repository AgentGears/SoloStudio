from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class JobAdmission:
    job_id: str
    attempt_id: str
    reused: bool
    cost_id: str | None = None


@dataclass(frozen=True, slots=True)
class WorkerRunResult:
    job_id: str
    attempt_id: str
    attempt_state: str
    job_state: str
    exit_code: int
