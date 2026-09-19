from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> str: ...


class SystemClock:
    def now(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class FixedClock:
    def __init__(self, value: str = "2026-09-19T20:00:00.000Z") -> None:
        self.value = value

    def now(self) -> str:
        return self.value
