from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Sequence

from solostudio.kernel.errors import InvalidArtifact, InvalidCommand
from solostudio.kernel.identity import canonical_text
from solostudio.kernel.jobs.models import WorkerRunResult
from solostudio.kernel.jobs.service import JobService


class SupervisedMediaWorker:
    def __init__(self, jobs: JobService) -> None:
        self.jobs = jobs
        self._lock = threading.Lock()

    def run(self, attempt_id: str, command: Sequence[str], *, timeout_seconds: float = 300.0) -> WorkerRunResult:
        if not command:
            raise InvalidCommand("worker command must not be empty")
        with self._lock:
            temp_dir = self.jobs.start_attempt(attempt_id, "local-media-worker")
            billing_ambiguous_on_interrupt = False
            try:
                request = self.jobs.request_payload(attempt_id)
                billing_ambiguous_on_interrupt = bool(
                    request.get("route", {}).get("billing_ambiguity_on_interrupt", False)
                )
                (temp_dir / "request.json").write_text(canonical_text(request), encoding="utf-8")
                completed = subprocess.run(
                    list(command),
                    cwd=temp_dir,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                job_state = self.jobs.fail_attempt(
                    attempt_id,
                    "WORKER_TIMEOUT",
                    f"worker exceeded timeout of {timeout_seconds} seconds",
                    billing_ambiguous=billing_ambiguous_on_interrupt,
                )
                return WorkerRunResult(
                    self.jobs.attempt(attempt_id)["job_id"],
                    attempt_id,
                    "FAILED",
                    job_state,
                    -1,
                )
            except OSError as exc:
                job_state = self.jobs.fail_attempt(attempt_id, "WORKER_START_FAILED", str(exc))
                return WorkerRunResult(
                    self.jobs.attempt(attempt_id)["job_id"],
                    attempt_id,
                    "FAILED",
                    job_state,
                    -1,
                )
            if completed.returncode != 0:
                interrupted_by_signal = completed.returncode < 0
                job_state = self.jobs.fail_attempt(
                    attempt_id,
                    "WORKER_PROCESS_FAILED",
                    _bounded_error(completed.stderr or completed.stdout or f"exit {completed.returncode}"),
                    billing_ambiguous=(
                        billing_ambiguous_on_interrupt and interrupted_by_signal
                    ),
                )
                return WorkerRunResult(
                    self.jobs.attempt(attempt_id)["job_id"],
                    attempt_id,
                    "FAILED",
                    job_state,
                    completed.returncode,
                )

            result_path = temp_dir / "result.json"
            if not result_path.is_file():
                job_state = self.jobs.fail_attempt(attempt_id, "WORKER_RESULT_MISSING", "worker exited successfully without result.json")
                return WorkerRunResult(self.jobs.attempt(attempt_id)["job_id"], attempt_id, "FAILED", job_state, 0)
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                job_state = self.jobs.fail_attempt(attempt_id, "WORKER_RESULT_INVALID", str(exc))
                return WorkerRunResult(self.jobs.attempt(attempt_id)["job_id"], attempt_id, "FAILED", job_state, 0)
            if not isinstance(result, dict) or result.get("status") != "SUCCEEDED":
                job_state = self.jobs.fail_attempt(
                    attempt_id,
                    str(result.get("error_code", "WORKER_REPORTED_FAILURE")) if isinstance(result, dict) else "WORKER_RESULT_INVALID",
                    str(result.get("error_message", "worker did not report success")) if isinstance(result, dict) else "worker result must be an object",
                )
                return WorkerRunResult(self.jobs.attempt(attempt_id)["job_id"], attempt_id, "FAILED", job_state, 0)

            job = self.jobs.job(self.jobs.attempt(attempt_id)["job_id"])
            try:
                if job["job_class"] == "STATE_PROPOSAL":
                    proposal = result.get("proposal")
                    if not isinstance(proposal, dict):
                        raise InvalidCommand("state worker result requires proposal")
                    self.jobs.complete_state_proposal(attempt_id, proposal)
                else:
                    outputs = result.get("outputs")
                    if not isinstance(outputs, list):
                        raise InvalidArtifact("artifact worker result requires outputs list")
                    self.jobs.complete_artifact_attempt(attempt_id, outputs)
            except (InvalidArtifact, InvalidCommand, OSError) as exc:
                job_state = self.jobs.fail_attempt(attempt_id, getattr(exc, "code", "WORKER_OUTPUT_INVALID"), str(exc))
                return WorkerRunResult(job["id"], attempt_id, "FAILED", job_state, 0)
            return WorkerRunResult(job["id"], attempt_id, "SUCCEEDED", "SUCCEEDED", 0)


def _bounded_error(value: str, limit: int = 2000) -> str:
    value = value.strip()
    return value if len(value) <= limit else value[:limit] + "…"
