"""SQLite for structured personal data (expenses, and whatever comes next).

* One file on a Docker volume (SQLITE_PATH), WAL mode, foreign keys on.
* Migrations are an ordered list of SQL scripts; ``PRAGMA user_version``
  stores how many have been applied. Never edit an applied migration;
  append a new one instead.
* sqlite3 is blocking, so each operation runs in a thread with its own
  short-lived connection.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")

MIGRATIONS: list[str] = [
    # 1: expenses
    """
    CREATE TABLE expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
        category TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        spent_at TEXT NOT NULL,   -- ISO 8601, UTC
        created_at TEXT NOT NULL  -- ISO 8601, UTC
    );
    CREATE INDEX idx_expenses_spent_at ON expenses (spent_at);
    CREATE INDEX idx_expenses_category ON expenses (category);
    """,
]


class Database:
    def __init__(self, path: Path, migrations: list[str] | None = None) -> None:
        self.path = path
        self.migrations = MIGRATIONS if migrations is None else migrations

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            with conn:  # commit on success, rollback on error
                yield conn
        finally:
            conn.close()

    def migrate(self) -> int:
        """Apply pending migrations; returns how many were applied."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            pending = self.migrations[current:]
            for number, script in enumerate(pending, start=current + 1):
                conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
            return len(pending)

    def version(self) -> int:
        with self.connect() as conn:
            return conn.execute("PRAGMA user_version").fetchone()[0]

    async def run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run ``fn(conn)`` in a worker thread inside a transaction."""

        def job() -> T:
            with self.connect() as conn:
                return fn(conn)

        return await asyncio.to_thread(job)
