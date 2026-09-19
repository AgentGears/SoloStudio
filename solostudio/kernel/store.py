from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from solostudio.kernel.clock import Clock
from solostudio.kernel.migrations import MIGRATIONS


class KernelStore:
    def __init__(self, path: Path, clock: Clock, busy_timeout_ms: int = 5000) -> None:
        self.path = path
        self.clock = clock
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _migrate(self) -> None:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                applied = {
                    int(row[0])
                    for row in self._connection.execute("SELECT version FROM schema_migrations")
                }
                for version, script in MIGRATIONS:
                    if version in applied:
                        continue
                    for statement in _statements(script):
                        self._connection.execute(statement)
                    self._connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                        (version, self.clock.now()),
                    )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._connection


def _statements(script: str) -> list[str]:
    buffer = ""
    statements: list[str] = []
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        buffer += line + "\n"
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""
    if buffer.strip():
        raise ValueError("incomplete migration statement")
    return statements
