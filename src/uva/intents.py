"""Standing intents: "when X comes up, do Y" (event-based prospective memory).

An intent has a text (what to do) and triggers: a list of entries, each a list
of terms; an entry matches a message when **all** its terms are in it
(normalized like the similarity tier: lowercase, no accents, simple plural
folding). Only the user's own messages are checked (never notices, routines
or goals), with no LLM: an FTS prefilter over the armed intents, then the exact
check. A hit puts "Standing intent (created <date>): <text>" in the message's
context block and counts a fire.

States: ``armed``; ``fired`` (in cooldown, armed again when it ends); ``done``
(fires used up); ``cancelled``; ``expired``. Defaults: 24 h cooldown, 3
fires, 90 days. Expiry and the end of a cooldown are applied on read and by
the daily maintenance (no tick of their own).

``scope = "results"`` intents ("quando aparecer uma promoção do jogo X numa
pesquisa, me avisa") are checked against research summaries and goal results
instead of messages.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from uva.clock import format_local, utcnow
from uva.text import normalize

ARMED, FIRED, DONE, CANCELLED, EXPIRED = "armed", "fired", "done", "cancelled", "expired"
LIVE = (ARMED, FIRED)
MESSAGES, RESULTS = "messages", "results"
DEFAULT_COOLDOWN_HOURS = 24
DEFAULT_MAX_FIRES = 3
DEFAULT_EXPIRES_DAYS = 90
MAX_CANDIDATES = 256
MAX_PER_MESSAGE = 3


def fold(word: str) -> str:
    """Simple plural folding: "promocoes" -> "promocao", "jogos" -> "jogo", "games" -> "game"."""
    if len(word) <= 3 or not word.endswith("s"):
        return word
    for suffix, repl in (("oes", "ao"), ("aes", "ao"), ("ais", "al"), ("eis", "el"), ("ois", "ol"), ("ns", "m")):
        if word.endswith(suffix):
            return word[: -len(suffix)] + repl
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("res", "zes", "ses")) and len(word) > 4:
        return word[:-2]
    return word[:-1]


def words(text: str) -> list[str]:
    return [fold(w) for w in normalize(text).split()]


def term_words(term: str) -> list[str]:
    return words(term)


def normalize_triggers(raw) -> list[list[str]]:
    """Entries of folded terms; a string entry is one term (it may have spaces)."""
    entries = []
    for entry in raw or []:
        terms = [entry] if isinstance(entry, str) else list(entry)
        clean = [" ".join(term_words(str(t))) for t in terms if str(t).strip()]
        clean = [t for t in clean if t]
        if clean:
            entries.append(clean)
    return entries


def matches(entries: list[list[str]], text: str) -> bool:
    """True when every term of some entry is in ``text`` (whole words)."""
    padded = f" {' '.join(words(text))} "
    return any(all(f" {term} " in padded for term in entry) for entry in entries)


@dataclass(slots=True)
class Intent:
    id: int
    text: str
    triggers: list[list[str]]
    state: str
    created_at: datetime
    expires_at: datetime | None
    max_fires: int
    fires: int
    cooldown_s: int
    last_fired_at: datetime | None
    source_trace: str | None
    scope: str = MESSAGES

    def describe_triggers(self) -> str:
        return " | ".join(" + ".join(entry) for entry in self.triggers)


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _intent(row) -> Intent:
    return Intent(
        id=row["id"], text=row["text"], triggers=json.loads(row["triggers_json"]), state=row["state"],
        created_at=_dt(row["created_at"]), expires_at=_dt(row["expires_at"]), max_fires=row["max_fires"],
        fires=row["fires"], cooldown_s=row["cooldown_s"], last_fired_at=_dt(row["last_fired_at"]),
        source_trace=row["source_trace"], scope=row["scope"],
    )


def _refresh_sql(conn, now: datetime) -> int:
    """Expire old intents and re-arm those whose cooldown is over."""
    iso = now.isoformat()
    changed = conn.execute(
        "UPDATE intents SET state = 'expired' WHERE state IN ('armed', 'fired') AND expires_at IS NOT NULL "
        "AND expires_at <= ?", (iso,),
    ).rowcount
    for row in conn.execute("SELECT id, last_fired_at, cooldown_s FROM intents WHERE state = 'fired'").fetchall():
        last = _dt(row["last_fired_at"])
        if last is None or last + timedelta(seconds=row["cooldown_s"]) <= now:
            conn.execute("UPDATE intents SET state = 'armed' WHERE id = ?", (row["id"],))
            changed += 1
    return changed


class IntentStore:
    """SQLite ``intents`` + ``intents_fts`` (migration 11)."""

    def __init__(self, db) -> None:
        self.db = db

    async def create(
        self, text: str, triggers, *, max_fires: int = DEFAULT_MAX_FIRES,
        cooldown_hours: float = DEFAULT_COOLDOWN_HOURS, expires_days: float | None = DEFAULT_EXPIRES_DAYS,
        scope: str = MESSAGES, source_trace: str | None = None, now: datetime | None = None,
    ) -> Intent:
        entries = normalize_triggers(triggers)
        if not entries:
            raise ValueError("no triggers")
        now = now or utcnow()
        expires = (now + timedelta(days=expires_days)).isoformat() if expires_days else None
        flat = " ".join(term for entry in entries for term in entry)

        def job(conn):
            cursor = conn.execute(
                "INSERT INTO intents (text, triggers_json, state, created_at, expires_at, max_fires, fires, "
                "cooldown_s, scope, source_trace) VALUES (?, ?, 'armed', ?, ?, ?, 0, ?, ?, ?)",
                (text.strip(), json.dumps(entries, ensure_ascii=False), now.isoformat(), expires,
                 max(1, int(max_fires)), max(0, int(cooldown_hours * 3600)), scope, source_trace),
            )
            intent_id = int(cursor.lastrowid)
            conn.execute("INSERT INTO intents_fts (rowid, triggers) VALUES (?, ?)", (intent_id, flat))
            row = conn.execute("SELECT * FROM intents WHERE id = ?", (intent_id,)).fetchone()
            return _intent(row)

        return await self.db.run(job)

    async def get(self, intent_id: int) -> Intent | None:
        row = await self.db.run(lambda conn: conn.execute("SELECT * FROM intents WHERE id = ?", (intent_id,)).fetchone())
        return _intent(row) if row else None

    async def list(self, now: datetime | None = None, *, include_closed: bool = True) -> list[Intent]:
        now = now or utcnow()

        def job(conn):
            _refresh_sql(conn, now)
            where = "" if include_closed else "WHERE state IN ('armed', 'fired')"
            return [_intent(r) for r in conn.execute(f"SELECT * FROM intents {where} ORDER BY id").fetchall()]

        return await self.db.run(job)

    async def cancel(self, intent_id: int) -> Intent | None:
        def job(conn):
            row = conn.execute("SELECT * FROM intents WHERE id = ?", (intent_id,)).fetchone()
            if row is None or row["state"] not in LIVE:
                return None
            conn.execute("UPDATE intents SET state = 'cancelled' WHERE id = ?", (intent_id,))
            conn.execute("DELETE FROM intents_fts WHERE rowid = ?", (intent_id,))
            return _intent(row)

        return await self.db.run(job)

    async def maintenance(self, now: datetime | None = None) -> int:
        return await self.db.run(lambda conn: _refresh_sql(conn, now or utcnow()))

    async def fire(self, text: str, *, scope: str = MESSAGES, now: datetime | None = None,
                   limit: int = MAX_PER_MESSAGE) -> list[Intent]:
        """The armed intents ``text`` triggers (at most ``limit``), counted as fired."""
        now = now or utcnow()
        tokens = sorted(set(words(text)))
        if not tokens:
            return []
        query = " OR ".join(f'"{token}"' for token in tokens)

        def job(conn):
            _refresh_sql(conn, now)
            rows = conn.execute(
                "SELECT i.* FROM intents_fts f JOIN intents i ON i.id = f.rowid "
                "WHERE intents_fts MATCH ? AND i.state = 'armed' AND i.scope = ? ORDER BY i.id LIMIT ?",
                (query, scope, MAX_CANDIDATES),
            ).fetchall()
            fired = []
            for row in rows:
                intent = _intent(row)
                if not matches(intent.triggers, text):
                    continue
                fires = intent.fires + 1
                state = DONE if fires >= intent.max_fires else FIRED
                conn.execute(
                    "UPDATE intents SET fires = ?, last_fired_at = ?, state = ? WHERE id = ?",
                    (fires, now.isoformat(), state, intent.id),
                )
                if state == DONE:
                    conn.execute("DELETE FROM intents_fts WHERE rowid = ?", (intent.id,))
                intent.fires, intent.last_fired_at, intent.state = fires, now, state
                fired.append(intent)
                if len(fired) >= limit:
                    break
            return fired

        return await self.db.run(job)


def store_for(services) -> IntentStore | None:
    return IntentStore(services.sqlite) if services.sqlite is not None else None


def render_fired(intents: list[Intent], tz) -> str:
    """For the context block (the LLM reads it)."""
    return "\n".join(
        f"Standing intent #{i.id} (created {format_local(i.created_at, tz, '%d/%m/%Y')}): {i.text}" for i in intents
    )



async def results_notice(services, text: str) -> str:
    """Lines for the user when a research or goal result triggers "results" intents."""
    from uva.i18n import t

    store = store_for(services)
    if store is None or not text.strip():
        return ""
    try:
        fired = await store.fire(text, scope=RESULTS)
    except Exception:
        return ""
    return "\n".join(t("intents.result_hit", id=i.id, text=i.text) for i in fired)
