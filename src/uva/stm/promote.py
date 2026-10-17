"""Promotion: when a session goes idle, keep what matters in long-term memory.

A beat task asks the cheap model which facts from the conversation are worth
remembering (memory_save), then discards the session. Refs that are still
running are kept so their notifications still have a referent.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import BaseModel, Field, ValidationError

from uva.clock import utcnow
from uva.llm.base import LLMError, LLMMessage
from uva.llm.budget import Scope
from uva.logging.events import EventType, emit, trace
from uva.memory.store import MemoryCategory
from uva.services import Services
from uva.stm.session import Session

SYSTEM = (
    "You review a conversation between a personal assistant (UVA) and its user. "
    "List only lasting facts about the user that are worth remembering for weeks "
    "(profile, preferences, people, routine, projects). Ignore one-off requests, "
    "jokes and what is already in memory. If nothing is worth it, return an empty list. "
    'Answer ONLY with JSON: {"memories": [{"key": "short_key", "value": "...", '
    '"category": "perfil|preferencia|fato|contexto", "relevance": 0.0-1.0}]}'
)


class Candidate(BaseModel):
    key: str = Field(min_length=1)
    value: str = Field(min_length=1)
    category: MemoryCategory = MemoryCategory.FATO
    relevance: float = Field(default=0.5, ge=0, le=1)


class Promotion(BaseModel):
    memories: list[Candidate] = Field(default_factory=list)


async def promote_idle_sessions(services: Services, now: datetime | None = None) -> dict[str, int]:
    """Check every session; returns counters for the log."""
    now = now or utcnow()
    stm = services.stm
    if stm is None:
        return {"sessions": 0}
    idle = timedelta(hours=services.settings.stm_promote_idle_hours)
    result = {"sessions": 0, "promoted": 0, "saved": 0, "skipped": 0}
    for user_id in await stm.backend.users():
        session = await stm.backend.load(user_id)
        if session is None or now - session.last_activity < idle:
            continue
        result["sessions"] += 1
        with trace():
            saved = await promote(services, user_id, session)
        if saved is None:
            result["skipped"] += 1
        else:
            result["promoted"] += 1
            result["saved"] += saved
    return result


async def promote(services: Services, user_id: int, session: Session) -> int | None:
    """Save worthwhile facts and discard the session. None = try again later."""
    assert services.stm is not None
    user_turns = [t for t in session.turns if t.role == "user"]
    saved = 0
    if user_turns and services.llm is not None and services.memory is not None:
        if services.budget is not None and not (await services.budget.check(Scope.PROACTIVE)).allowed:
            return None
        known = ", ".join(i.key for i in await services.memory.working_memory()) or "(nothing)"
        transcript = "\n".join(
            f"{'User' if t.role == 'user' else 'UVA'}: {t.text}" for t in session.turns
        )
        try:
            response = await services.llm.complete(
                [LLMMessage("user", f"Already in memory: {known}\n\nConversation:\n{transcript}")],
                purpose="stm_promotion",
                scope=Scope.PROACTIVE,
                system=SYSTEM,
                json_schema=Promotion.model_json_schema(),
                use_soul=False,
            )
            promotion = Promotion.model_validate_json(response.text)
        except (LLMError, ValidationError, ValueError) as exc:
            await emit(EventType.ERROR, where="stm_promotion", error=str(exc))
            return None
        for item in promotion.memories:
            await services.memory.save(
                item.key, item.value, category=item.category, relevance=item.relevance,
                source="stm_promotion",
            )
            saved += 1

    running = [r for r in session.refs if r.active]

    def keep_running(s: Session) -> None:
        s.turns, s.refs = [], running
        s.focus = running[-1].ref if running else None

    if running:
        await services.stm.backend.update(user_id, keep_running, services.stm.ttl)
    else:
        await services.stm.backend.delete(user_id)
    await emit(EventType.MEMORY, op="stm_promotion", saved=saved, kept_running=len(running))
    return saved
