"""Failure incidents of routines: the same error alerts once, not every run.

A failure's signature is its error text normalized (lowercase, numbers and
durations stripped), so "timeout after 31s" and "timeout after 58s" are the
same incident. An open incident alerts when it starts and reminds every
ROUTINE_FAILURE_REMIND_HOURS; a successful run resolves the routine's open
incidents (the next failure alerts again).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from uva.clock import utcnow
from uva.storage.sqlite import Database

SIGNATURE_CHARS = 200
_NUMBERS = re.compile(r"\d+(?:[.,:]\d+)*\s*(?:ms|s|sec|secs|seconds|segundos|min|mins|minutes|minutos|h|hours|horas)?\b")
_HEX = re.compile(r"\b[0-9a-f]{6,}\b")


def signature(error: str) -> str:
    text = error.lower()
    text = _HEX.sub("#", text)
    text = _NUMBERS.sub("#", text)
    return " ".join(text.split())[:SIGNATURE_CHARS] or "error"


@dataclass(frozen=True, slots=True)
class Incident:
    routine_id: str
    signature: str
    state: str  # "open" | "resolved"
    first_at: datetime
    last_alert_at: datetime | None
    count: int
    last_error: str


class IncidentStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def failed(self, routine_id: str, error: str, remind_after: timedelta,
                     now: datetime | None = None) -> tuple[bool, Incident]:
        """Record a failure; returns (alert the user now?, the incident)."""
        now = now or utcnow()
        sig = signature(error)

        def job(conn: sqlite3.Connection) -> tuple[bool, Incident]:
            row = conn.execute(
                "SELECT * FROM routine_incidents WHERE routine_id = ? AND signature = ? AND state = 'open'",
                (routine_id, sig),
            ).fetchone()
            if row is None:
                cursor = conn.execute(
                    "INSERT INTO routine_incidents "
                    "(routine_id, signature, state, first_at, last_alert_at, count, last_error) "
                    "VALUES (?, ?, 'open', ?, ?, 1, ?)",
                    (routine_id, sig, now.isoformat(), now.isoformat(), error[:500]),
                )
                incident_id, alert = cursor.lastrowid, True
            else:
                last = datetime.fromisoformat(row["last_alert_at"]) if row["last_alert_at"] else None
                alert = last is None or now - last >= remind_after
                incident_id = row["id"]
                conn.execute(
                    "UPDATE routine_incidents SET count = count + 1, last_error = ?"
                    + (", last_alert_at = ?" if alert else "")
                    + " WHERE id = ?",
                    (error[:500], *((now.isoformat(),) if alert else ()), incident_id),
                )
            found = conn.execute("SELECT * FROM routine_incidents WHERE id = ?", (incident_id,)).fetchone()
            return alert, _incident(found)

        return await self.db.run(job)

    async def succeeded(self, routine_id: str) -> int:
        """Resolve the routine's open incidents; returns how many."""

        def job(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                "UPDATE routine_incidents SET state = 'resolved' WHERE routine_id = ? AND state = 'open'",
                (routine_id,),
            )
            return cursor.rowcount

        return await self.db.run(job)

    async def list(self, routine_id: str, limit: int = 10) -> list[Incident]:
        def job(conn: sqlite3.Connection) -> list[Incident]:
            rows = conn.execute(
                "SELECT * FROM routine_incidents WHERE routine_id = ? "
                "ORDER BY state = 'open' DESC, id DESC LIMIT ?",
                (routine_id, limit),
            ).fetchall()
            return [_incident(row) for row in rows]

        return await self.db.run(job)


def _incident(row: sqlite3.Row) -> Incident:
    return Incident(
        routine_id=row["routine_id"],
        signature=row["signature"],
        state=row["state"],
        first_at=datetime.fromisoformat(row["first_at"]),
        last_alert_at=datetime.fromisoformat(row["last_alert_at"]) if row["last_alert_at"] else None,
        count=row["count"],
        last_error=row["last_error"],
    )
