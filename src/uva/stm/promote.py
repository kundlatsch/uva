"""Expiry of idle sessions: when a session goes idle, it is dropped.

What was worth keeping already moved on: every turn is in the conversation
log (SQLite), and the background review (uva/review.py) writes lasting facts
to memory and skills as the conversation goes. Refs that are still running
are kept so their notifications still have a referent.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from uva.clock import utcnow
from uva.logging.events import EventType, emit, trace
from uva.services import Services
from uva.stm.session import Session


async def promote_idle_sessions(services: Services, now: datetime | None = None) -> dict[str, int]:
    """Drop every session idle for STM_PROMOTE_IDLE_HOURS; returns counters for the log."""
    now = now or utcnow()
    stm = services.stm
    if stm is None:
        return {"sessions": 0}
    idle = timedelta(hours=services.settings.stm_promote_idle_hours)
    result = {"sessions": 0, "dropped": 0}
    for user_id in await stm.backend.users():
        session = await stm.backend.load(user_id)
        if session is None or now - session.last_activity < idle:
            continue
        result["sessions"] += 1
        with trace():
            await drop(services, user_id, session)
        result["dropped"] += 1
    return result


async def drop(services: Services, user_id: int, session: Session) -> None:
    """Discard the session, keeping only refs still running."""
    assert services.stm is not None
    running = [r for r in session.refs if r.active]

    def keep_running(s: Session) -> None:
        s.turns, s.refs = [], running
        s.focus = running[-1].ref if running else None

    if running:
        await services.stm.backend.update(user_id, keep_running, services.stm.ttl)
    else:
        await services.stm.backend.delete(user_id)
    await emit(EventType.MEMORY, op="stm_expired", turns=len(session.turns), kept_running=len(running))
