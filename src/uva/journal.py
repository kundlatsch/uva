"""Inbound journal: what the bot was answering when it stopped.

Every turn is written as ``received`` before it is routed, ``processing`` once
it starts, and ``done`` / ``failed`` after the reply went out (SQLite
``inbound``, migration 12). When the bot starts, rows still ``received`` or
``processing`` and younger than RECOVER_MAX_HOURS each get one question
("I was shut down while answering '...'. Do it again?") with Sim / Não; a
turn is never re-run on its own (it may have had side effects). Older ones
are marked failed quietly. The daily maintenance deletes rows older than a
week.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from uva.clock import utcnow

RECEIVED, PROCESSING, DONE, FAILED = "received", "processing", "done", "failed"
KEEP = timedelta(days=7)
MAX_QUESTIONS = 5
TEXT_KINDS = ("text",)  # what can be redone from its text alone


@dataclass(frozen=True, slots=True)
class Entry:
    id: int
    update_id: int | None
    chat_id: int
    message_id: int | None
    kind: str  # text | button | photo | voice | document | sticker
    text: str
    state: str
    received_at: datetime
    done_at: datetime | None


def _entry(row) -> Entry:
    return Entry(
        row["id"], row["update_id"], row["chat_id"], row["message_id"], row["kind"], row["text"], row["state"],
        datetime.fromisoformat(row["received_at"]),
        datetime.fromisoformat(row["done_at"]) if row["done_at"] else None,
    )


class InboundJournal:
    def __init__(self, db) -> None:
        self.db = db

    async def received(
        self, chat_id: int, kind: str, text: str, *, message_id: int | None = None,
        update_id: int | None = None, now: datetime | None = None,
    ) -> int:
        at = (now or utcnow()).isoformat()
        return await self.db.run(lambda conn: int(conn.execute(
            "INSERT INTO inbound (update_id, chat_id, message_id, kind, text, state, received_at) "
            "VALUES (?, ?, ?, ?, ?, 'received', ?)", (update_id, chat_id, message_id, kind, text, at),
        ).lastrowid))

    async def mark(self, entry_id: int, state: str, now: datetime | None = None) -> None:
        done_at = (now or utcnow()).isoformat() if state in (DONE, FAILED) else None
        await self.db.run(lambda conn: conn.execute(
            "UPDATE inbound SET state = ?, done_at = COALESCE(?, done_at) WHERE id = ?", (state, done_at, entry_id),
        ))

    async def get(self, entry_id: int) -> Entry | None:
        row = await self.db.run(lambda conn: conn.execute("SELECT * FROM inbound WHERE id = ?", (entry_id,)).fetchone())
        return _entry(row) if row else None

    async def interrupted(self, now: datetime | None = None, max_hours: float = 6) -> list[Entry]:
        """Turns cut short by a shutdown, young enough to ask about (oldest first).
        Every unfinished row is closed: the young ones are asked about once, the
        old ones are just marked failed."""
        now = now or utcnow()
        cutoff = now - timedelta(hours=max_hours)

        def job(conn):
            rows = [_entry(r) for r in conn.execute(
                "SELECT * FROM inbound WHERE state IN ('received', 'processing') ORDER BY id"
            ).fetchall()]
            if rows:
                conn.execute(
                    "UPDATE inbound SET state = 'failed', done_at = ? WHERE state IN ('received', 'processing')",
                    (now.isoformat(),),
                )
            return [r for r in rows if r.received_at >= cutoff][-MAX_QUESTIONS:]

        return await self.db.run(job)

    async def purge(self, now: datetime | None = None) -> int:
        cutoff = ((now or utcnow()) - KEEP).isoformat()
        return await self.db.run(lambda conn: conn.execute(
            "DELETE FROM inbound WHERE received_at < ?", (cutoff,)
        ).rowcount)


def journal_for(services) -> InboundJournal | None:
    return InboundJournal(services.sqlite) if services.sqlite is not None else None


def recovery_question(entry: Entry):
    """The message asking whether to redo an interrupted turn."""
    from uva.i18n import t
    from uva.replies import Button, Reply

    shown = entry.text if len(entry.text) <= 80 else entry.text[:79] + "…"
    if entry.kind not in TEXT_KINDS:
        return Reply(t("main.recover.media", kind=t(f"main.recover.kind.{entry.kind}"), text=shown))
    return Reply(
        t("main.recover.ask", text=shown),
        buttons=[Button(t("main.recover.yes"), f"rcvok:{entry.id}"), Button(t("main.recover.no"), f"rcvno:{entry.id}")],
    )
