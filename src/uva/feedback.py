"""Reactions on UVA's messages, used as feedback (all rule-based, no LLM).

The user's emoji reactions on messages UVA sent on its own are kept in SQLite
``reactions`` with what the message was (``sent_messages``: an initiative, a
routine or goal report, a research result, a review notice). Then:

* 👎 on an initiative blocks its topic for INITIATIVE_BLOCK_DAYS: the chooser
  refuses it, asks once more, and then sends nothing;
* 👍 / ❤️ on an initiative make its topic one he liked (named in the prompt);
* 👎 on a routine or goal report tells the next run that the last output was
  not liked;
* the background review sees reactions next to the turns.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from uva import sent
from uva.clock import utcnow
from uva.text import normalize

DISLIKE = frozenset({"👎", "💩", "😡", "🤮"})
LIKE = frozenset({"👍", "❤", "❤️", "🔥", "😍", "🥰", "👏", "💯"})
KEEP = timedelta(days=90)


@dataclass(frozen=True, slots=True)
class Reaction:
    message_id: int
    emoji: str
    at: datetime
    kind: str
    ref: str
    topic: str


def normalize_topic(topic: str) -> str:
    return " ".join(normalize(topic).split())


def _reaction(row) -> Reaction:
    return Reaction(row["message_id"], row["emoji"], datetime.fromisoformat(row["at"]), row["kind"], row["ref"],
                    row["topic"])


async def record(services, message_id: int, emojis: list[str], now: datetime | None = None) -> list[Reaction]:
    """Keep new reactions on one of UVA's messages; returns what was kept."""
    db = services.sqlite
    if db is None or not emojis:
        return []
    now = now or utcnow()
    found = await sent.lookup(db, message_id)
    kind, ref = (found.kind, found.ref) if found else ("", "")
    topic = await _topic(services, kind, ref)
    rows = [(message_id, emoji, now.isoformat(), kind, ref, topic) for emoji in emojis]
    await db.run(lambda conn: conn.executemany(
        "INSERT INTO reactions (message_id, emoji, at, kind, ref, topic) VALUES (?, ?, ?, ?, ?, ?)", rows,
    ))
    return [Reaction(message_id, e, now, kind, ref, topic) for e in emojis]


async def _topic(services, kind: str, ref: str) -> str:
    """An initiative's topic (or the start of its message), normalized."""
    if kind != "initiative" or services.initiatives is None or not ref:
        return ""
    try:
        for record_ in await services.initiatives.recent(100):
            if record_.id == ref:
                return normalize_topic(record_.topic or record_.message[:80])
    except Exception:
        return ""
    return ""


async def topics(db, emojis: frozenset[str], since: datetime) -> list[str]:
    if db is None:
        return []
    rows = await db.run(lambda conn: conn.execute(
        "SELECT DISTINCT topic, emoji FROM reactions WHERE kind = 'initiative' AND topic != '' AND at >= ?",
        (since.isoformat(),),
    ).fetchall())
    return sorted({row["topic"] for row in rows if row["emoji"] in emojis})


async def blocked_topics(services, now: datetime | None = None) -> list[str]:
    days = services.settings.initiative_block_days
    return await topics(services.sqlite, DISLIKE, (now or utcnow()) - timedelta(days=days))


async def liked_topics(services, now: datetime | None = None) -> list[str]:
    return await topics(services.sqlite, LIKE, (now or utcnow()) - timedelta(days=90))


async def disliked_since(db, kind: str, ref: str, since: datetime | None) -> bool:
    """A 👎 on a message about ``ref`` after ``since`` (its last run)."""
    if db is None or not ref:
        return False
    args = (kind, ref, (since or datetime(1970, 1, 1)).isoformat())
    rows = await db.run(lambda conn: conn.execute(
        "SELECT emoji FROM reactions WHERE kind = ? AND ref = ? AND at >= ?", args,
    ).fetchall())
    return any(row["emoji"] in DISLIKE for row in rows)


async def between(db, since: datetime, until: datetime) -> list[Reaction]:
    if db is None:
        return []
    rows = await db.run(lambda conn: conn.execute(
        "SELECT * FROM reactions WHERE at >= ? AND at <= ? ORDER BY at", (since.isoformat(), until.isoformat()),
    ).fetchall())
    return [_reaction(row) for row in rows]


async def purge(db, now: datetime | None = None) -> int:
    if db is None:
        return 0
    cutoff = ((now or utcnow()) - KEEP).isoformat()
    return await db.run(lambda conn: conn.execute("DELETE FROM reactions WHERE at < ?", (cutoff,)).rowcount)
