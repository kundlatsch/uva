"""Full-text index over everything UVA knows, plus the conversation log.

* ``search_index`` (SQLite FTS5, accent-insensitive) holds brain notes,
  memory items, goals and conversation turns. It is derived: files, Mongo and
  ``conversation_turns`` stay the source of truth, and :meth:`rebuild`
  recreates it (``/reindexar``).
* ``index_state`` keeps each note's mtime and hash, so :meth:`sync_notes`
  (beat, every 10 minutes) picks up edits made in Obsidian and drops deleted
  notes.
* ``conversation_turns`` is append-only: every user message, reply and notice,
  with where it came from. Redis keeps only the live 12h window.

sqlite3 is blocking: the async methods run in a thread (Database.run); the
vault hooks are synchronous because the vault itself is.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from uva.clock import utcnow
from uva.logging import get_logger
from uva.storage.sqlite import Database
from uva.text import normalize

if TYPE_CHECKING:
    from uva.brain.vault import Vault
    from uva.goals.store import Goal
    from uva.memory.store import MemoryItem

log = get_logger(__name__)

Kind = Literal["note", "memory", "goal", "turn"]
KINDS: tuple[Kind, ...] = ("note", "memory", "goal", "turn")
TurnSource = Literal["telegram", "dashboard", "notice", "routine", "goal", "initiative"]
TURN_ROLES = ("user", "bot", "notice")

# Where a notice comes from: routines, goals and initiatives set it around
# their runs, so the log tells them apart from task notices.
notice_source: ContextVar[TurnSource] = ContextVar("uva_notice_source", default="notice")
# Where a conversation turn comes from (the dashboard chat sets "dashboard").
turn_source: ContextVar[TurnSource] = ContextVar("uva_turn_source", default="telegram")

# bm25 column weights: kind, ref, title, body, updated_at.
TITLE_WEIGHT = 5.0
BODY_WEIGHT = 1.0
MAX_BODY = 20000


@dataclass(frozen=True, slots=True)
class Hit:
    kind: Kind
    ref: str
    title: str
    snippet: str
    updated_at: str
    rank: float
    body: str = ""


@dataclass(frozen=True, slots=True)
class TurnRecord:
    id: int
    at: datetime
    role: str
    text: str
    source: str
    trace_id: str | None = None


def match_query(words: Iterable[str]) -> str:
    """FTS5 query: any of the words, each as a prefix ("marmita"* also finds
    "marmitas"). Words are normalized and quoted, so no operator gets through."""
    terms = []
    for word in words:
        word = normalize(word)
        for part in word.split():
            if part and f'"{part}"*' not in terms:
                terms.append(f'"{part}"*')
    return " OR ".join(terms)


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class SearchIndex:
    def __init__(self, db: Database, vault: Vault | None = None) -> None:
        self.db = db
        self.vault = vault

    # --- writing (sync helpers, used inside a connection) ------------------------

    @staticmethod
    def _put(conn: sqlite3.Connection, kind: str, ref: str, title: str, body: str, updated_at: str) -> None:
        conn.execute("DELETE FROM search_index WHERE kind = ? AND ref = ?", (kind, ref))
        conn.execute(
            "INSERT INTO search_index (kind, ref, title, body, updated_at) VALUES (?, ?, ?, ?, ?)",
            (kind, ref, title, body[:MAX_BODY], updated_at),
        )

    @staticmethod
    def _drop(conn: sqlite3.Connection, kind: str, ref: str) -> None:
        conn.execute("DELETE FROM search_index WHERE kind = ? AND ref = ?", (kind, ref))
        conn.execute("DELETE FROM index_state WHERE kind = ? AND ref = ?", (kind, ref))

    # --- notes (sync: called by the vault right after it writes) ------------------

    def note_changed(self, path: str) -> None:
        """Index one note now (vault hook). Never raises."""
        if self.vault is None:
            return
        try:
            with self.db.connect() as conn:
                self._index_note(conn, path)
        except Exception as exc:
            log.warning("index_note_failed", path=path, error=str(exc))

    def note_removed(self, path: str) -> None:
        try:
            with self.db.connect() as conn:
                self._drop(conn, "note", path)
        except Exception as exc:
            log.warning("index_note_failed", path=path, error=str(exc))

    def _index_note(self, conn: sqlite3.Connection, path: str) -> None:
        from uva.brain.vault import parse_note

        assert self.vault is not None
        file = self.vault.root / path
        if not file.is_file():
            self._drop(conn, "note", path)
            return
        text = file.read_text(encoding="utf-8", errors="replace")
        mtime = file.stat().st_mtime
        meta, body = parse_note(text)
        title = str(meta.get("title") or file.stem)
        updated = datetime.fromtimestamp(mtime).astimezone().isoformat(timespec="seconds")
        self._put(conn, "note", path, title, body, updated)
        conn.execute(
            "INSERT OR REPLACE INTO index_state (kind, ref, mtime, hash) VALUES ('note', ?, ?, ?)",
            (path, mtime, _hash(text)),
        )

    def _note_paths(self) -> list[str]:
        from uva.brain.vault import README_NAME

        assert self.vault is not None
        root = self.vault.root
        if not root.is_dir():
            return []
        return [
            self.vault._relative(p)
            for p in sorted(root.rglob("*.md"))
            if p.name != README_NAME and self.vault._visible(p)
        ]

    def _sync_notes(self) -> dict[str, int]:
        if self.vault is None:
            return {"indexed": 0, "removed": 0}
        paths = self._note_paths()
        indexed = removed = 0
        with self.db.connect() as conn:
            known = {
                row["ref"]: row["mtime"]
                for row in conn.execute("SELECT ref, mtime FROM index_state WHERE kind = 'note'")
            }
            for path in paths:
                try:
                    mtime = (self.vault.root / path).stat().st_mtime
                except OSError:
                    continue
                if known.get(path) != mtime:
                    self._index_note(conn, path)
                    indexed += 1
            for path in set(known) - set(paths):
                self._drop(conn, "note", path)
                removed += 1
        return {"indexed": indexed, "removed": removed}

    async def sync_notes(self) -> dict[str, int]:
        """Re-index notes whose mtime changed and drop deleted ones."""
        import asyncio

        return await asyncio.to_thread(self._sync_notes)

    # --- memory and goals -------------------------------------------------------------

    async def index_memory(self, item: MemoryItem) -> None:
        def job(conn: sqlite3.Connection) -> None:
            body = f"{item.key.replace('_', ' ')} {item.value}"
            self._put(conn, "memory", item.key, item.key, body, item.last_used_at.isoformat())

        await self.db.run(job)

    async def remove_memory(self, key: str) -> None:
        await self.db.run(lambda conn: self._drop(conn, "memory", key))

    async def index_goal(self, goal: Goal) -> None:
        def job(conn: sqlite3.Connection) -> None:
            results = "\n".join(r.summary for r in goal.results[-5:])
            body = f"{goal.description}\n{results}".strip()
            self._put(conn, "goal", goal.id, goal.title, body, utcnow().isoformat())

        await self.db.run(job)

    # --- conversation log ---------------------------------------------------------------

    async def add_turn(
        self,
        role: str,
        text: str,
        *,
        source: str | None = None,
        trace_id: str | None = None,
        at: datetime | None = None,
    ) -> int:
        """Append one turn to the log (and to the index). Returns its id."""
        if source is None:
            source = notice_source.get() if role == "notice" else turn_source.get()
        at = (at or utcnow()).astimezone(UTC)

        def job(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                "INSERT INTO conversation_turns (at, role, text, trace_id, source) VALUES (?, ?, ?, ?, ?)",
                (at.isoformat(), role, text, trace_id, source),
            )
            turn_id = int(cursor.lastrowid)
            self._put(conn, "turn", str(turn_id), f"{role} {source}", text, at.isoformat())
            return turn_id

        return await self.db.run(job)

    async def add_exchange(
        self, user_text: str, bot_text: str, *, trace_id: str | None = None, source: str | None = None
    ) -> None:
        await self.add_turn("user", user_text, trace_id=trace_id, source=source)
        await self.add_turn("bot", bot_text, trace_id=trace_id, source=source)

    async def turns(
        self, since: datetime | None = None, until: datetime | None = None, limit: int = 50
    ) -> list[TurnRecord]:
        """Turns in a period, oldest first (the latest ``limit``)."""

        def job(conn: sqlite3.Connection) -> list[TurnRecord]:
            where, args = _period(since, until)
            rows = conn.execute(
                f"SELECT * FROM conversation_turns {where} ORDER BY id DESC LIMIT ?", (*args, limit)
            ).fetchall()
            return [_turn(row) for row in reversed(rows)]

        return await self.db.run(job)

    async def last_turn_id(self) -> int:
        return await self.db.run(
            lambda conn: conn.execute("SELECT COALESCE(MAX(id), 0) FROM conversation_turns").fetchone()[0]
        )

    async def search_turns(
        self,
        words: list[str],
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 10,
    ) -> list[TurnRecord]:
        """Turns matching any of the words in a period, best first."""
        query = match_query(words)
        if not query:
            return []

        def job(conn: sqlite3.Connection) -> list[TurnRecord]:
            where, args = _period(since, until, column="t.at")
            where = where.replace("WHERE", "AND", 1)
            rows = conn.execute(
                f"""
                SELECT t.* FROM search_index s
                JOIN conversation_turns t ON t.id = CAST(s.ref AS INTEGER)
                WHERE search_index MATCH ? AND s.kind = 'turn' {where}
                ORDER BY bm25(search_index, 0, 0, {TITLE_WEIGHT}, {BODY_WEIGHT}, 0) LIMIT ?
                """,
                (query, *args, limit),
            ).fetchall()
            return [_turn(row) for row in rows]

        return await self.db.run(job)

    # --- searching --------------------------------------------------------------------------

    async def search(
        self, words: list[str] | str, kinds: Iterable[str] | None = None, limit: int = 10
    ) -> list[Hit]:
        """Best matches for any of ``words`` (bm25, title weighs more)."""
        if isinstance(words, str):
            words = normalize(words).split()
        query = match_query(words)
        if not query:
            return []
        kinds = tuple(kinds or KINDS)

        def job(conn: sqlite3.Connection) -> list[Hit]:
            marks = ", ".join("?" for _ in kinds)
            rows = conn.execute(
                f"""
                SELECT kind, ref, title, body, updated_at,
                       snippet(search_index, 3, '', '', '…', 16) AS snip,
                       bm25(search_index, 0, 0, {TITLE_WEIGHT}, {BODY_WEIGHT}, 0) AS rank
                FROM search_index WHERE search_index MATCH ? AND kind IN ({marks})
                ORDER BY rank LIMIT ?
                """,
                (query, *kinds, limit),
            ).fetchall()
            return [
                Hit(r["kind"], r["ref"], r["title"], r["snip"], r["updated_at"], r["rank"], r["body"])
                for r in rows
            ]

        return await self.db.run(job)

    async def count(self, kind: str | None = None) -> int:
        def job(conn: sqlite3.Connection) -> int:
            if kind is None:
                return conn.execute("SELECT COUNT(*) FROM search_index").fetchone()[0]
            return conn.execute("SELECT COUNT(*) FROM search_index WHERE kind = ?", (kind,)).fetchone()[0]

        return await self.db.run(job)

    # --- rebuild ------------------------------------------------------------------------------

    async def rebuild(
        self, memory: Iterable[MemoryItem] = (), goals: Iterable[Goal] = ()
    ) -> dict[str, int]:
        """Drop the index and build it again from the sources of truth."""
        memory, goals = list(memory), list(goals)

        def job() -> dict[str, int]:
            with self.db.connect() as conn:
                conn.execute("DELETE FROM search_index")
                conn.execute("DELETE FROM index_state")
                for item in memory:
                    body = f"{item.key.replace('_', ' ')} {item.value}"
                    self._put(conn, "memory", item.key, item.key, body, item.last_used_at.isoformat())
                for goal in goals:
                    results = "\n".join(r.summary for r in goal.results[-5:])
                    self._put(conn, "goal", goal.id, goal.title, f"{goal.description}\n{results}".strip(),
                              utcnow().isoformat())
                turns = 0
                for row in conn.execute("SELECT * FROM conversation_turns ORDER BY id").fetchall():
                    self._put(conn, "turn", str(row["id"]), f"{row['role']} {row['source']}", row["text"], row["at"])
                    turns += 1
            notes = self._sync_notes()["indexed"]
            return {"notes": notes, "memory": len(memory), "goals": len(goals), "turns": turns}

        import asyncio

        return await asyncio.to_thread(job)


def _period(
    since: datetime | None, until: datetime | None, column: str = "at"
) -> tuple[str, tuple[str, ...]]:
    # Stored as UTC ISO strings, so the bounds are compared the same way.
    clauses, args = [], []
    if since is not None:
        clauses.append(f"{column} >= ?")
        args.append(since.astimezone(UTC).isoformat())
    if until is not None:
        clauses.append(f"{column} < ?")
        args.append(until.astimezone(UTC).isoformat())
    return ("WHERE " + " AND ".join(clauses) if clauses else ""), tuple(args)


def _turn(row: sqlite3.Row) -> TurnRecord:
    return TurnRecord(
        id=row["id"],
        at=datetime.fromisoformat(row["at"]),
        role=row["role"],
        text=row["text"],
        source=row["source"],
        trace_id=row["trace_id"],
    )
