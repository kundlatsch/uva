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
    # 2: stickers (meaning and optional action per sticker)
    """
    CREATE TABLE stickers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        unique_id TEXT NOT NULL UNIQUE,  -- Telegram file_unique_id
        file_id TEXT NOT NULL DEFAULT '',
        emoji TEXT NOT NULL DEFAULT '',
        set_name TEXT NOT NULL DEFAULT '',
        meaning TEXT NOT NULL DEFAULT '',
        action TEXT NOT NULL DEFAULT '',
        uses INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    # 3: conversation log (append-only) and the full-text index (derived, see uva/index)
    """
    CREATE TABLE conversation_turns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,            -- ISO 8601, UTC
        role TEXT NOT NULL,          -- user | bot | notice
        text TEXT NOT NULL,
        trace_id TEXT,
        source TEXT NOT NULL         -- telegram | dashboard | notice | routine | goal | initiative
    );
    CREATE INDEX idx_turns_at ON conversation_turns (at);
    CREATE VIRTUAL TABLE search_index USING fts5(
        kind UNINDEXED, ref UNINDEXED, title, body, updated_at UNINDEXED,
        tokenize='unicode61 remove_diacritics 2'
    );
    CREATE TABLE index_state (
        kind TEXT NOT NULL,
        ref TEXT NOT NULL,
        mtime REAL,
        hash TEXT,
        PRIMARY KEY (kind, ref)
    );
    """,
    # 4: skill usage counters (the skills themselves are files in data/skills)
    """
    CREATE TABLE skill_usage (
        name TEXT PRIMARY KEY,
        uses INTEGER NOT NULL DEFAULT 0,
        last_used_at TEXT
    );
    """,
    # 5: small key/value state (last reviewed turn, last journal digest...)
    """
    CREATE TABLE kv (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # 6: routine failure incidents (one alert per distinct error, see uva/incidents.py)
    """
    CREATE TABLE routine_incidents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        routine_id TEXT NOT NULL,
        signature TEXT NOT NULL,
        state TEXT NOT NULL,         -- open | resolved
        first_at TEXT NOT NULL,
        last_alert_at TEXT,
        count INTEGER NOT NULL DEFAULT 1,
        last_error TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX idx_incidents_routine ON routine_incidents (routine_id, state);
    """,
    # 7: /sh and /py commands the user said to always allow (exact text)
    """
    CREATE TABLE shell_allowlist (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,          -- sh | py
        code TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (kind, code)
    );
    """,
    # 8: command usage (plugin curator) and undone turns (/desfazer_turno)
    """
    CREATE TABLE command_usage (
        name TEXT PRIMARY KEY,
        uses INTEGER NOT NULL DEFAULT 0,
        last_used_at TEXT
    );
    ALTER TABLE conversation_turns ADD COLUMN undone INTEGER NOT NULL DEFAULT 0;
    """,
    # 9: messages UVA sent on its own, by Telegram message id (what a quoted reply
    # or a reaction refers to; see uva/sent.py)
    """
    CREATE TABLE sent_messages (
        message_id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL DEFAULT '',   -- task | routine | goal | initiative | review | notice
        ref TEXT NOT NULL DEFAULT '',    -- task, routine or goal id ('' when none)
        at TEXT NOT NULL
    );
    CREATE INDEX idx_sent_at ON sent_messages (at);
    """,
    # 10: where each turn came from (uva/provenance.py): owner | agent | external | system
    """
    ALTER TABLE conversation_turns ADD COLUMN origin TEXT NOT NULL DEFAULT 'owner';
    UPDATE conversation_turns SET origin = 'agent' WHERE role = 'bot';
    UPDATE conversation_turns SET origin = 'system' WHERE role = 'notice' OR source IN ('routine', 'goal', 'initiative');
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

    async def get_kv(self, key: str) -> str | None:
        row = await self.run(lambda conn: conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone())
        return row[0] if row else None

    async def set_kv(self, key: str, value: str) -> None:
        await self.run(
            lambda conn: conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))
        )

    async def run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run ``fn(conn)`` in a worker thread inside a transaction."""

        def job() -> T:
            with self.connect() as conn:
                return fn(conn)

        return await asyncio.to_thread(job)
