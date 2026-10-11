"""/log: inspect recorded events by period, type or trace."""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

from uva.clock import format_local, start_of_day, utcnow
from uva.commands.registry import CommandContext, CommandRegistry
from uva.logging.events import Event, EventQuery, EventType
from uva.replies import Reply
from uva.text import extract_duration, extract_keywords

USAGE = (
    "Uso: /log [período] [tipo]  ou  /log trace <id>\n"
    "Período: 30m, 6h, 2d, hoje, ontem (padrão: 1h)\n"
    "Tipo: llm, goals, erros, tarefas, memoria, acoes"
)

FILTERS: dict[str, set[EventType]] = {
    "llm": {EventType.LLM_CALL},
    "goals": {EventType.GOAL_RUN},
    "objetivos": {EventType.GOAL_RUN},
    "erros": {EventType.ERROR},
    "errors": {EventType.ERROR},
    "tarefas": {EventType.TASK_ENQUEUED, EventType.TASK_FINISHED},
    "memoria": {EventType.MEMORY},
    "acoes": {EventType.ACTION},
}

_DURATION = re.compile(r"^(\d+)(m|h|d)$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


class LogArgsError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LogRequest:
    query: EventQuery
    label: str  # human description of the period/filter
    detail: bool = False  # True for "/log trace <id>"


def parse_log_args(args: list[str], tz: tzinfo, now: datetime | None = None) -> LogRequest:
    now = now or utcnow()
    words = [a.lower() for a in args]

    if words and words[0] == "trace":
        if len(words) != 2:
            raise LogArgsError(USAGE)
        return LogRequest(EventQuery(trace_id=words[1]), f"trace {words[1]}", detail=True)

    since, until, period = now - timedelta(hours=1), None, "última hora"
    types: set[EventType] | None = None
    filter_name = ""

    for word in words:
        if match := _DURATION.match(word):
            amount, unit = int(match[1]), match[2]
            since, until, period = now - timedelta(**{_UNITS[unit]: amount}), None, word
        elif word == "hoje":
            since, until, period = start_of_day(tz, now), None, "hoje"
        elif word == "ontem":
            today = start_of_day(tz, now)
            since, until, period = today - timedelta(days=1), today, "ontem"
        elif word in FILTERS:
            types, filter_name = FILTERS[word], word
        else:
            raise LogArgsError(f"Não entendi '{word}'.\n{USAGE}")

    label = period + (f", {filter_name}" if filter_name else "")
    return LogRequest(EventQuery(since=since, until=until, types=types), label)


# Free-text words that map to /log arguments ("log de erros hoje").
_KEYWORDS = {
    "hoje": "hoje",
    "ontem": "ontem",
    "llm": "llm",
    "ia": "llm",
    "erro": "erros",
    "erros": "erros",
    "objetivo": "goals",
    "objetivos": "goals",
    "goals": "goals",
    "tarefa": "tarefas",
    "tarefas": "tarefas",
    "memoria": "memoria",
    "acao": "acoes",
    "acoes": "acoes",
}


def extract_log_args(normalized: str) -> tuple[str, list[str]]:
    """'log de erros das ultimas 2 horas' -> ('log de das', ['2h', 'erros'])."""
    remaining, duration = extract_duration(normalized)
    remaining, keywords = extract_keywords(remaining, _KEYWORDS)
    return remaining, ([duration] if duration else []) + keywords


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "log",
        "Mostra o que aconteceu (eventos)",
        aliases=("logs", "eventos"),
        usage="[período] [tipo] | trace <id>",
        examples=("o que aconteceu", "historico de eventos", "ultimos eventos"),
        extract_args=extract_log_args,
    )
    async def log_command(ctx: CommandContext) -> str | Reply:
        store = ctx.services.events
        if store is None:
            return "O log de eventos não está disponível (Mongo não configurado)."
        try:
            request = parse_log_args(ctx.args, ctx.services.tz)
        except LogArgsError as exc:
            return str(exc)

        events = await store.find(request.query)
        tz = ctx.services.tz
        if not events:
            return f"Nenhum evento ({request.label})."
        if request.detail:
            text = format_trace(events, tz)
        elif request.query.types:
            text = format_event_list(events, tz, request.label)
        else:
            text = format_summary(events, tz, request.label)
        return Reply(text=text, filename="log.md").fit_for_telegram()


# --- formatting -------------------------------------------------------------


def format_summary(events: list[Event], tz: tzinfo, label: str) -> str:
    """One line per trace, showing the path each message took."""
    traces: OrderedDict[str, list[Event]] = OrderedDict()
    for event in events:
        traces.setdefault(event.trace_id, []).append(event)

    lines = [f"Log ({label}): {len(traces)} rastros, {len(events)} eventos"]
    for trace_id, group in traces.items():
        lines.append(_summarize_trace(trace_id, group, tz))
    lines.append("\nDetalhes: /log trace <id>")
    return "\n".join(lines)


def _summarize_trace(trace_id: str, group: list[Event], tz: tzinfo) -> str:
    head = f"{format_local(group[0].ts, tz, '%d/%m %H:%M:%S')} [{trace_id}]"
    parts: list[str] = []
    actions: list[str] = []
    llm_calls, llm_cost = 0, 0.0

    for event in group:
        data = event.data
        match event.type:
            case EventType.MESSAGE_RECEIVED:
                parts.append(f'"{_preview(data.get("text", ""))}"')
            case EventType.ROUTE:
                parts.append(_describe_route(data))
            case EventType.ACTION:
                actions.append(str(data.get("name")))
            case EventType.LLM_CALL:
                llm_calls += 1
                llm_cost += float(data.get("cost_usd") or 0)
            case EventType.TASK_ENQUEUED:
                parts.append(f"tarefa {data.get('action')} enfileirada")
            case EventType.TASK_FINISHED:
                parts.append(f"tarefa {data.get('status')}")
            case EventType.GOAL_RUN:
                reason = f", {data['reason']}" if data.get("reason") else ""
                parts.append(f"objetivo '{data.get('title')}' ({data.get('status')}{reason})")
            case EventType.ERROR:
                parts.append(f"ERRO: {_preview(str(data.get('error', '')), 60)}")

    if actions:
        parts.append("ações: " + ", ".join(actions))
    if llm_calls:
        parts.append(f"llm: {llm_calls}x ${llm_cost:.4f}")
    return f"{head} " + (" → ".join(parts) if parts else group[0].type.value)


def _describe_route(data: dict) -> str:
    tier = data.get("tier")
    command = data.get("command")
    match tier:
        case "command":
            return f"comando /{command}"
        case "similarity":
            return f"similaridade /{command} ({data.get('score', 0):.0f})"
        case "confirm":
            return f"confirmação /{command}? ({data.get('score', 0):.0f})"
        case "llm":
            actions = [a.get("type") for a in data.get("plan", {}).get("actions", [])]
            return "LLM" + (f" ({', '.join(actions)})" if actions else "")
        case "llm_disabled":
            return "LLM desligada"
        case "similarity_miss":
            return f"sem comando parecido (melhor: /{command} {data.get('score', 0):.0f})"
        case "confirmed":
            return f"confirmado /{command}"
        case "confirm_denied":
            return f"recusado /{command}"
        case "confirm_expired":
            return "confirmação expirada"
        case "none":
            return "não entendido"
        case "unknown_command":
            return f"comando desconhecido /{command}"
        case _:
            return str(tier)


def format_event_list(events: list[Event], tz: tzinfo, label: str) -> str:
    lines = [f"Log ({label}): {len(events)} eventos"]
    for event in events:
        details = " ".join(f"{k}={_preview(str(v), 80)}" for k, v in event.data.items())
        when = format_local(event.ts, tz, "%d/%m %H:%M:%S")
        lines.append(f"{when} [{event.trace_id}] {event.type.value} {details}")
    return "\n".join(lines)


def format_trace(events: list[Event], tz: tzinfo) -> str:
    lines = [f"Trace {events[0].trace_id}: {len(events)} eventos"]
    for event in events:
        when = format_local(event.ts, tz, "%d/%m %H:%M:%S")
        lines.append(f"\n{when} {event.type.value}")
        if event.data:
            lines.append(json.dumps(event.data, ensure_ascii=False, indent=2))
    return "\n".join(lines)


def _preview(text: str, limit: int = 40) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
