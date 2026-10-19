"""Messages UVA sent on its own (task results, routine and goal reports,
initiatives, review notices), by Telegram message id.

When the user quotes one of them ("refaz isso") the router moves the focus to
the task it was about; reactions on them are read the same way. Kept for
:data:`KEEP`; the daily maintenance deletes older rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from uva.clock import utcnow

KEEP = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class Sent:
    message_id: int
    kind: str
    ref: str
    at: datetime


async def record(db, message_ids: list[int], kind: str, ref: str = "", now: datetime | None = None) -> None:
    if db is None or not message_ids:
        return
    at = (now or utcnow()).isoformat()
    rows = [(int(m), kind, ref, at) for m in message_ids]
    await db.run(lambda conn: conn.executemany(
        "INSERT OR REPLACE INTO sent_messages (message_id, kind, ref, at) VALUES (?, ?, ?, ?)", rows,
    ))


async def lookup(db, message_id: int) -> Sent | None:
    if db is None:
        return None
    row = await db.run(lambda conn: conn.execute(
        "SELECT message_id, kind, ref, at FROM sent_messages WHERE message_id = ?", (int(message_id),),
    ).fetchone())
    if row is None:
        return None
    return Sent(row["message_id"], row["kind"], row["ref"], datetime.fromisoformat(row["at"]))


async def purge(db, now: datetime | None = None) -> int:
    if db is None:
        return 0
    cutoff = ((now or utcnow()) - KEEP).isoformat()
    return await db.run(lambda conn: conn.execute("DELETE FROM sent_messages WHERE at < ?", (cutoff,)).rowcount)
