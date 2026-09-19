from __future__ import annotations

from collections import defaultdict
from typing import Protocol
from uuid import uuid4


class IdSource(Protocol):
    def new(self, prefix: str) -> str: ...


class RandomIdSource:
    def new(self, prefix: str) -> str:
        return f"{prefix}_{uuid4().hex}"


class SequenceIdSource:
    def __init__(self) -> None:
        self._counters: dict[str, int] = defaultdict(int)

    def new(self, prefix: str) -> str:
        self._counters[prefix] += 1
        return f"{prefix}_{self._counters[prefix]:04d}"
