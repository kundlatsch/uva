"""Initiatives: messages the bot sends on its own, based on what it knows about you.

Every few minutes the beat calls :func:`initiative_tick`. If messages are on,
it isn't quiet hours, the daily cap and minimum gap allow it, nobody is
chatting and the proactive budget has room, the cheap model looks at the
long-term memory, the recent conversation, the brain and the goals, and picks
one of:
  * research - search a topic you care about and send the summary (also saved
    to 03-recursos);
  * message  - a reminder, a follow-up question or a nudge, in the soul's voice;
  * nothing  - when there is nothing worth interrupting you for.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Literal, Protocol

from pydantic import BaseModel, Field, ValidationError

from uva.actions.search import ResearchError, research, save_research
from uva.brain.naming import portuguese_title
from uva.clock import format_local, start_of_day, utcnow
from uva.goals.runner import in_quiet_hours
from uva.goals.store import GoalStatus
from uva.i18n import language_name, t
from uva.llm.base import LLMError, LLMMessage
from uva.llm.budget import Scope
from uva.logging import get_logger
from uva.logging.events import EventType, emit, trace
from uva.memory.service import render_memory
from uva.replies import Reply
from uva.services import Services

log = get_logger(__name__)

Kind = Literal["research", "message", "nothing"]


class InitiativeRecord(BaseModel):
    id: str = Field(default_factory=lambda: secrets.token_hex(3))
    at: datetime = Field(default_factory=utcnow)
    kind: Kind
    topic: str = ""
    message: str = ""
    note: str | None = None


class InitiativeStore(Protocol):
    async def add(self, record: InitiativeRecord) -> None: ...

    async def recent(self, limit: int) -> list[InitiativeRecord]:
        """Newest first."""
        ...

    async def count_since(self, since: datetime) -> int: ...

    async def get_enabled(self) -> bool | None:
        """Runtime override set with /proativo (None = use the .env default)."""
        ...

    async def set_enabled(self, enabled: bool) -> None: ...


class Decision(BaseModel):
    kind: Kind = Field(description="research, message or nothing")
    topic: str = Field(default="", description="For research: what to look up, in a few words")
    message: str = Field(
        default="", description="Text for the user (for research, an opening sentence)"
    )
    reason: str = Field(default="", description="Why this, in one sentence (goes to the log)")
    title_pt: str = Field(default="", description="For research: short note title in Brazilian Portuguese")


SYSTEM = """You decide whether it is worth sending, RIGHT NOW, a message on your own initiative
to Gustavo, your only user, on Telegram. Options:
- "research": look up something he is interested in (based on memory, conversations, notes
  and goals) and send a summary. Pick concrete, varied topics.
- "message": a reminder, a follow-up question about something he told you or started (a
  book, a project, a goal) or a useful suggestion. Short.
- "nothing": only when there is really nothing. He WANTS to hear from you several times a
  day; alternate research about his interests and short messages.
Don't repeat topics of recent initiatives. Active goals are already being worked on by
themselves: don't ask about or research what they cover. Don't make up facts about him.
"topic" in English; "message" (what he reads) in {language}, technical terms in
English; "title_pt" (for research): a short Brazilian Portuguese title for the note.
Answer with ONLY JSON: {{"kind": "...", "topic": "...", "message": "...", "reason": "...", "title_pt": "..."}}"""


def system_prompt() -> str:
    return SYSTEM.format(language=language_name())


async def is_enabled(services: Services) -> bool:
    store = services.initiatives
    override = await store.get_enabled() if store is not None else None
    return services.settings.proactive_messages_enabled if override is None else override


async def gate(services: Services, now: datetime) -> str | None:
    """Why an initiative can't happen now, or None if it can."""
    s = services.settings
    store = services.initiatives
    if store is None or services.llm is None:
        return "disabled"
    if not await is_enabled(services):
        return "off"
    if in_quiet_hours(services, now):
        return "quiet"
    if await store.count_since(start_of_day(services.tz, now)) >= s.proactive_max_messages_per_day:
        return "daily_cap"
    recent = await store.recent(1)
    if recent and now - recent[0].at < timedelta(minutes=s.proactive_min_gap_minutes):
        return "gap"
    last_check = await _last_check(store)
    if last_check is not None and now - last_check < timedelta(minutes=s.proactive_decide_every_minutes):
        return "waiting"  # asked the model recently; asking every tick wastes budget
    if services.busy is not None and await services.busy.reasons():
        return "busy"  # includes a goal run in progress ("goal:<id>")
    if services.goals is not None and await services.goals.due(now):
        return "goal_due"  # goals go first; an initiative would just talk over them
    return None


async def _last_check(store) -> datetime | None:
    getter = getattr(store, "get_last_check", None)
    return await getter() if getter is not None else None


async def _set_last_check(store, now: datetime) -> None:
    setter = getattr(store, "set_last_check", None)
    if setter is not None:
        await setter(now)


async def initiative_tick(
    services: Services, now: datetime | None = None, *, force: bool = False
) -> tuple[str, str | None]:
    """One turn. Returns (status, message to send or None).

    ``force`` (from /proativo agora) skips the on/off, quiet-hours, cap and gap
    gates, but never the budget.
    """
    now = now or utcnow()
    if not force and (blocked := await gate(services, now)) is not None:
        return blocked, None
    if services.llm is None or services.initiatives is None:
        return "disabled", None

    with trace():
        if services.budget is not None:
            status = await services.budget.check(Scope.PROACTIVE)
            if not status.allowed:
                await emit(EventType.INITIATIVE, status="skipped", reason=status.reason)
                return "budget", None
        await _set_last_check(services.initiatives, now)
        try:
            decision = await _decide(services, now)
        except (LLMError, ValidationError, ValueError) as exc:
            await emit(EventType.ERROR, where="initiative", error=str(exc))
            return "error", None

        if decision.kind == "nothing":
            await emit(EventType.INITIATIVE, status="nothing", reason=decision.reason)
            return "nothing", None

        record = InitiativeRecord(kind=decision.kind, topic=decision.topic, message=decision.message)
        if decision.kind == "research":
            if not decision.topic.strip():
                return "nothing", None
            try:
                found = await research(services, decision.topic, "web", Scope.PROACTIVE)
            except ResearchError as exc:
                await emit(EventType.ERROR, where="initiative_research", error=str(exc))
                return "error", None
            if services.vault is not None:
                title = await portuguese_title(
                    services, decision.title_pt, found.summary, scope=Scope.PROACTIVE,
                    alternatives=(decision.topic,),
                )
                record.note = save_research(services.vault, found, title).path
            opening = decision.message.strip() or t("initiatives.opening")
            text = f"🧠 {opening}\n\n{found.render()}"
            if record.note:
                text += f"\n\n📝 {record.note}"
        else:
            if not decision.message.strip():
                return "nothing", None
            text = f"💬 {decision.message.strip()}"

        record.message = text
        await services.initiatives.add(record)
        await emit(
            EventType.INITIATIVE, status="sent", kind=decision.kind, topic=decision.topic,
            reason=decision.reason, note=record.note,
        )
        return "sent", text


async def _decide(services: Services, now: datetime) -> Decision:
    assert services.llm is not None
    context = await _context(services, now)
    response = await services.llm.complete(
        [LLMMessage("user", context)],
        purpose="initiative",
        scope=Scope.PROACTIVE,
        system=system_prompt(),
        json_schema=Decision.model_json_schema(),
        max_tokens=800,
    )
    text = response.text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    return Decision.model_validate_json(text)


async def _context(services: Services, now: datetime) -> str:
    tz = services.tz
    parts = [f"Now: {format_local(now, tz, '%A %d/%m %H:%M')}"]
    if services.memory is not None:
        try:
            memory = render_memory(await services.memory.working_memory())
        except Exception:
            memory = ""
        parts.append("Memory (what you know about him):\n" + (memory or "(empty)"))
    if services.stm is not None:
        try:
            session = await services.stm.session()
            turns = "\n".join(
                f"- {'he' if t.role == 'user' else 'you'}: {t.text[:300]}" for t in session.turns[-8:]
            )
            parts.append("Recent conversation:\n" + (turns or "(none)"))
        except Exception:
            pass
    if services.vault is not None:
        try:
            notes = services.vault.recent(8)
            parts.append("Recent second brain notes:\n" + "\n".join(f"- {n.path}" for n in notes))
        except Exception:
            pass
    if services.goals is not None:
        goals = await services.goals.list(GoalStatus.ATIVO)
        parts.append("Active goals:\n" + ("\n".join(f"- {g.title}" for g in goals) or "(none)"))
    if services.initiatives is not None:
        recent = await services.initiatives.recent(10)
        last = recent[0].at if recent else None
        hours = (now - last).total_seconds() / 3600 if last else None
        if hours is None or hours >= services.settings.proactive_force_after_hours:
            since = "never" if hours is None else f"{hours:.0f}h ago"
            parts.append(f"Last initiative: {since}. Do NOT choose \"nothing\" now: pick research or message.")
        lines = [f"- {format_local(r.at, tz, '%d/%m %H:%M')} {r.kind}: {r.topic or r.message[:80]}" for r in recent]
        parts.append("Your recent initiatives (don't repeat them):\n" + ("\n".join(lines) or "(none)"))
    return "\n\n".join(parts)


async def describe_status(services: Services, now: datetime | None = None) -> str:
    """Text for /proativo."""
    now = now or utcnow()
    s = services.settings
    tz = services.tz
    store = services.initiatives
    lines = [t("initiatives.status.title")]
    enabled = await is_enabled(services)
    state = t("initiatives.status.on" if enabled else "initiatives.status.off")
    lines.append(t("initiatives.status.enabled", state=state))
    if store is not None:
        today = await store.count_since(start_of_day(tz, now))
        lines.append(t(
            "initiatives.status.today", count=today, cap=s.proactive_max_messages_per_day,
            gap=s.proactive_min_gap_minutes,
        ))
    lines.append(
        t("initiatives.status.quiet", start=s.proactive_quiet_start_hour, end=s.proactive_quiet_end_hour)
        + (t("initiatives.status.quiet_now") if in_quiet_hours(services, now) else "")
    )
    lines.append(t(
        "initiatives.status.poll", poll=s.proactive_poll_minutes, interval=s.proactive_interval_minutes
    ))
    blocked = await gate(services, now)
    if blocked is None:
        next_one = t("initiatives.status.next_ready")
    elif blocked in ("off", "quiet", "daily_cap", "gap", "busy", "goal_due", "disabled", "waiting"):
        next_one = t(f"initiatives.status.blocked.{blocked}", minutes=s.proactive_decide_every_minutes)
    else:
        next_one = blocked
    lines.append(t("initiatives.status.next", reason=next_one))
    if services.goals is not None:
        active = await services.goals.list(GoalStatus.ATIVO)
        if active:
            nxt = min(active, key=lambda g: g.next_run_at)
            lines.append(t(
                "initiatives.status.goals", count=len(active), title=nxt.title,
                at=format_local(nxt.next_run_at, tz, '%d/%m %H:%M'),
            ))
        else:
            lines.append(t("initiatives.status.no_goals"))
    if store is not None:
        recent = await store.recent(5)
        if recent:
            lines.append("\n" + t("initiatives.status.recent"))
            lines += [f"- {format_local(r.at, tz, '%d/%m %H:%M')} {r.kind}: {(r.topic or r.message)[:70]}" for r in recent]
    lines.append("\n" + t("initiatives.status.now_hint"))
    return "\n".join(lines)


def reply_for(message: str | None, status: str) -> Reply:
    if message:
        return Reply(message)
    if status in ("nothing", "budget", "error", "disabled"):
        return Reply(t(f"initiatives.reply.{status}"))
    return Reply(status)
