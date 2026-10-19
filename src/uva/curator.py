"""Plugin and skill curator: what has not been used for a while steps aside.

Every command run is counted (SQLite ``command_usage``); skills are counted in
``skill_usage``. A plugin or skill unused for ``PLUGIN_STALE_DAYS`` (30) is
"idle": it drops out of /help, of the command list in the prompt and of the
skill index, but still works when called, and /features lists it as idle.
Nothing is ever deleted. A plugin or skill seen for the first time starts its
clock then, so new ones are not idle right away.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from uva.clock import utcnow
from uva.logging import get_logger

log = get_logger(__name__)

REFRESH_EVERY = timedelta(hours=1)


async def record_command(db, name: str) -> None:
    def job(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO command_usage (name, uses, last_used_at) VALUES (?, 1, ?) "
            "ON CONFLICT(name) DO UPDATE SET uses = uses + 1, last_used_at = excluded.last_used_at",
            (name, utcnow().isoformat()),
        )

    try:
        await db.run(job)
    except Exception as exc:  # counting must never break a command
        log.warning("command_usage_failed", command=name, error=str(exc))


def _idle(conn: sqlite3.Connection, table: str, names: list[str], cutoff: datetime, now: datetime) -> set[str]:
    rows = {r["name"]: r["last_used_at"] for r in conn.execute(f"SELECT name, last_used_at FROM {table}")}
    idle = set()
    for name in names:
        if name not in rows:  # first time seen: its clock starts now
            conn.execute(f"INSERT INTO {table} (name, uses, last_used_at) VALUES (?, 0, ?)", (name, now.isoformat()))
            continue
        last = rows[name]
        if last is not None and datetime.fromisoformat(last) < cutoff:
            idle.add(name)
    return idle


async def refresh_idle(services, now: datetime | None = None) -> tuple[set[str], set[str]]:
    """Recompute idle plugins and skills; sets registry.idle and skills.idle."""
    db = services.sqlite
    if db is None:
        return set(), set()
    now = now or utcnow()
    cutoff = now - timedelta(days=services.settings.plugin_stale_days)
    plugins = [c.name for c in services.registry if c.source == "plugin"]
    skills = [s.name for s in services.skills.all()] if services.skills is not None else []

    def job(conn: sqlite3.Connection) -> tuple[set[str], set[str]]:
        return _idle(conn, "command_usage", plugins, cutoff, now), _idle(conn, "skill_usage", skills, cutoff, now)

    idle_plugins, idle_skills = await db.run(job)
    services.registry.idle = idle_plugins
    if services.skills is not None:
        services.skills.idle = idle_skills
    return idle_plugins, idle_skills
