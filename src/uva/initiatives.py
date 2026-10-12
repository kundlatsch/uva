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
from uva.clock import format_local, start_of_day, utcnow
from uva.goals.runner import in_quiet_hours
from uva.goals.store import GoalStatus
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
    kind: Kind = Field(description="research, message ou nothing")
    topic: str = Field(default="", description="Para research: o que pesquisar, em poucas palavras")
    message: str = Field(
        default="", description="Texto para o usuário (para research, uma frase de abertura)"
    )
    reason: str = Field(default="", description="Por que isso, em uma frase (vai para o log)")


SYSTEM = """Você decide se vale a pena mandar AGORA uma mensagem por iniciativa própria
para o Gustavo, seu único usuário, no Telegram. Opções:
- "research": pesquisar algo do interesse dele (com base na memória, nas conversas, nas
  notas e nos objetivos) e mandar um resumo. Escolha temas concretos e variados.
- "message": um lembrete, uma pergunta de acompanhamento sobre algo que ele contou ou
  começou (um livro, um projeto, um objetivo) ou uma sugestão útil. Curta.
- "nothing": se não houver nada que valha a interrupção. Prefira isso a encher o saco.
Não repita temas das iniciativas recentes. Não invente fatos sobre ele. Responda SOMENTE com
JSON: {"kind": "...", "topic": "...", "message": "...", "reason": "..."}"""


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
    if services.busy is not None and await services.busy.reasons():
        return "busy"
    return None


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
                record.note = save_research(services.vault, found).path
            opening = decision.message.strip() or "Vi isso e lembrei de você:"
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
        system=SYSTEM,
        json_schema=Decision.model_json_schema(),
        max_tokens=800,
    )
    text = response.text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    return Decision.model_validate_json(text)


async def _context(services: Services, now: datetime) -> str:
    tz = services.tz
    parts = [f"Agora: {format_local(now, tz, '%A %d/%m %H:%M')}"]
    if services.memory is not None:
        try:
            memory = render_memory(await services.memory.working_memory())
        except Exception:
            memory = ""
        parts.append("Memória (o que você sabe dele):\n" + (memory or "(vazia)"))
    if services.stm is not None:
        try:
            session = await services.stm.session()
            turns = "\n".join(
                f"- {'ele' if t.role == 'user' else 'você'}: {t.text[:300]}" for t in session.turns[-8:]
            )
            parts.append("Conversa recente:\n" + (turns or "(nenhuma)"))
        except Exception:
            pass
    if services.vault is not None:
        try:
            notes = services.vault.recent(8)
            parts.append("Notas recentes do second brain:\n" + "\n".join(f"- {n.path}" for n in notes))
        except Exception:
            pass
    if services.goals is not None:
        goals = await services.goals.list(GoalStatus.ATIVO)
        parts.append("Objetivos ativos:\n" + ("\n".join(f"- {g.title}" for g in goals) or "(nenhum)"))
    if services.initiatives is not None:
        recent = await services.initiatives.recent(10)
        lines = [f"- {format_local(r.at, tz, '%d/%m %H:%M')} {r.kind}: {r.topic or r.message[:80]}" for r in recent]
        parts.append("Suas iniciativas recentes (não repita):\n" + ("\n".join(lines) or "(nenhuma)"))
    return "\n\n".join(parts)


async def describe_status(services: Services, now: datetime | None = None) -> str:
    """Text for /proativo."""
    now = now or utcnow()
    s = services.settings
    tz = services.tz
    store = services.initiatives
    lines = ["🤖 Proatividade"]
    enabled = await is_enabled(services)
    lines.append(f"Iniciativas: {'ligadas' if enabled else 'desligadas'} (/proativo on|off)")
    if store is not None:
        today = await store.count_since(start_of_day(tz, now))
        lines.append(f"Hoje: {today} de {s.proactive_max_messages_per_day} mensagens · intervalo mínimo {s.proactive_min_gap_minutes} min")
    lines.append(
        f"Silêncio: {s.proactive_quiet_start_hour}h–{s.proactive_quiet_end_hour}h"
        + (" (agora em silêncio)" if in_quiet_hours(services, now) else "")
    )
    lines.append(f"Verificação a cada {s.proactive_poll_minutes} min · objetivos no máximo a cada {s.proactive_interval_minutes} min")
    blocked = await gate(services, now)
    reasons = {
        "off": "desligadas", "quiet": "horário de silêncio", "daily_cap": "limite do dia atingido",
        "gap": "esperando o intervalo mínimo", "busy": "você está conversando", "disabled": "sem LLM",
    }
    lines.append("Próxima iniciativa: " + ("pode sair no próximo ciclo" if blocked is None else reasons.get(blocked, blocked)))
    if services.goals is not None:
        active = await services.goals.list(GoalStatus.ATIVO)
        if active:
            nxt = min(active, key=lambda g: g.next_run_at)
            lines.append(f"Objetivos ativos: {len(active)} · próximo: \"{nxt.title}\" às {format_local(nxt.next_run_at, tz, '%d/%m %H:%M')}")
        else:
            lines.append("Objetivos ativos: nenhum")
    if store is not None:
        recent = await store.recent(5)
        if recent:
            lines.append("\nÚltimas iniciativas:")
            lines += [f"- {format_local(r.at, tz, '%d/%m %H:%M')} {r.kind}: {(r.topic or r.message)[:70]}" for r in recent]
    lines.append("\n/proativo agora → manda uma iniciativa já (respeita só o orçamento)")
    return "\n".join(lines)


def reply_for(message: str | None, status: str) -> Reply:
    if message:
        return Reply(message)
    labels = {
        "nothing": "Olhei tudo e não achei nada que valesse te interromper agora.",
        "budget": "Sem orçamento proativo hoje (PROACTIVE_DAILY_BUDGET_USD).",
        "error": "Deu ruim ao pensar numa iniciativa (veja /log erros).",
        "disabled": "Iniciativas indisponíveis (sem LLM ou Mongo).",
    }
    return Reply(labels.get(status, status))
