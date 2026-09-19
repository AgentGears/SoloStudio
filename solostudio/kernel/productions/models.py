from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
