"""Routines: recurring requests ("todo dia 08:00 pesquisa se a skin X saiu na loja").

A routine is a request (what the user would type) plus a schedule: daily at a
time, on some weekdays at a time, or every N hours. The beat checks every
minute; a due routine goes through the normal router (same budget and
safeguards as a typed message) and the reply is sent to the user.

The PC is off at night, so slots are missed. When the bot comes back, every
routine whose time passed runs ONCE (not once per missed slot), and its next
run is the next slot after now.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, time, timedelta, tzinfo
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field

from uva.clock import format_local, utcnow
from uva.text import normalize


class Every(StrEnum):
    DAY = "day"  # every day at `at`
    WEEKDAYS = "weekdays"  # on `weekdays` at `at`
    HOURS = "hours"  # every `hours` hours


WEEKDAY_NAMES = ("segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo")
WEEKDAY_LABELS = ("seg", "ter", "qua", "qui", "sex", "sáb", "dom")


class Routine(BaseModel):
    id: str = Field(default_factory=lambda: "r" + secrets.token_hex(2))
    request: str  # what runs, as if the user typed it
    every: Every
    at: str = "08:00"  # local HH:MM (day / weekdays)
    weekdays: list[int] = Field(default_factory=list)  # 0 = Monday
    hours: float = 0  # interval (hours)
    enabled: bool = True
    created_at: datetime = Field(default_factory=utcnow)
    last_run_at: datetime | None = None
    next_run_at: datetime = Field(default_factory=utcnow)
    runs: int = 0
    last_result: str = ""


class RoutineStore(Protocol):
    async def add(self, routine: Routine) -> None: ...

    async def get(self, routine_id: str) -> Routine | None: ...

    async def update(self, routine_id: str, **fields: Any) -> None: ...

    async def delete(self, routine_id: str) -> bool: ...

    async def all(self) -> list[Routine]: ...

    async def due(self, now: datetime) -> list[Routine]: ...


def _at(value: str) -> time:
    hour, _, minute = value.partition(":")
    return time(int(hour), int(minute or 0))


def next_run(routine: Routine, after: datetime, tz: tzinfo) -> datetime:
    """The first slot strictly after ``after`` (UTC in, UTC out)."""
    if routine.every is Every.HOURS:
        step = timedelta(hours=max(routine.hours, 0.25))
        if routine.last_run_at is None:
            return after + step
        nxt = routine.last_run_at + step
        while nxt <= after:
            nxt += step
        return nxt
    local = after.astimezone(tz)
    at = _at(routine.at)
    days = set(routine.weekdays) if routine.every is Every.WEEKDAYS else set(range(7))
    for offset in range(0, 8):
        day = local.date() + timedelta(days=offset)
        if day.weekday() not in days:
            continue
        candidate = datetime.combine(day, at, tzinfo=tz)
        if candidate > local:
            return candidate.astimezone(after.tzinfo)
    raise ValueError("agenda sem dias")


def describe_schedule(routine: Routine) -> str:
    if routine.every is Every.DAY:
        return f"todo dia às {routine.at}"
    if routine.every is Every.WEEKDAYS:
        days = ", ".join(WEEKDAY_LABELS[d] for d in sorted(routine.weekdays))
        return f"{days} às {routine.at}"
    hours = f"{routine.hours:g}"
    return f"a cada {hours}h"


def describe(routine: Routine, tz: tzinfo) -> str:
    state = "" if routine.enabled else " (pausada)"
    lines = [f"{routine.id} · {describe_schedule(routine)}{state}", f"   {routine.request[:150]}"]
    nxt = format_local(routine.next_run_at, tz, "%d/%m %H:%M")
    last = format_local(routine.last_run_at, tz, "%d/%m %H:%M") if routine.last_run_at else "nunca"
    lines.append(f"   próxima: {nxt} · última: {last} · {routine.runs} execução(ões)")
    return "\n".join(lines)


# Normalized text: "08:00" became "08 00", "9h30" stays "9h30".
_TIME = r"(\d{1,2})(?:\s*h\s*|\s+)?(\d{2})?(?:\s*h(?:oras?)?)?(?=\s)"
_DAILY = re.compile(rf"^(?:todo\s+dia|todos\s+os\s+dias|diariamente)\s+(?:as\s+|a\s+)?{_TIME}\s+(?P<rest>.+)$")
_INTERVAL = re.compile(r"^a\s+cada\s+(\d+(?:[.,]\d+)?)\s*(h|hora|horas|min|minutos)\s+(?P<rest>.+)$")
_WEEKLY = re.compile(
    rf"^(?:toda|todo|todas|todos|nas?|aos?)?\s*(?P<days>(?:(?:segunda|terca|quarta|quinta|sexta|sabado|domingo)(?:-feira|s)?(?:\s*(?:,|e)\s*)?)+)"
    rf"\s+(?:as\s+|a\s+)?{_TIME}\s+(?P<rest>.+)$"
)


def parse(text: str) -> Routine | None:
    """'todo dia 08:00 pesquisa X' / 'toda segunda e quinta 9h faz Y' / 'a cada 6 horas Z'."""
    raw = text.strip()
    norm = normalize(raw)
    # Keep the original wording of the request: cut the schedule prefix by word count.
    def request_from(rest_norm: str) -> str:
        words = raw.split()
        return " ".join(words[len(words) - len(rest_norm.split()):])

    if m := _DAILY.match(norm):
        return Routine(request=request_from(m.group("rest")), every=Every.DAY, at=_hhmm(m.group(1), m.group(2)))
    if m := _INTERVAL.match(norm):
        amount = float(m.group(1).replace(",", "."))
        hours = amount / 60 if m.group(2).startswith("min") else amount
        if hours < 0.25:
            return None
        return Routine(request=request_from(m.group("rest")), every=Every.HOURS, hours=hours)
    if m := _WEEKLY.match(norm):
        days = sorted({WEEKDAY_NAMES.index(d) for d in re.findall("|".join(WEEKDAY_NAMES), m.group("days"))})
        if days:
            return Routine(
                request=request_from(m.group("rest")), every=Every.WEEKDAYS, weekdays=days,
                at=_hhmm(m.group(2), m.group(3)),  # group 1 is the days
            )
    return None


def _hhmm(hour: str, minute: str | None) -> str:
    h, mi = int(hour), int(minute or 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise ValueError("horário inválido")
    return f"{h:02d}:{mi:02d}"


async def schedule(store: RoutineStore, routine: Routine, tz: tzinfo, now: datetime | None = None) -> Routine:
    now = now or utcnow()
    routine.next_run_at = next_run(routine, now, tz)
    await store.add(routine)
    return routine


async def run_due(services, notify, now: datetime | None = None) -> list[str]:
    """Run each due routine once (missed slots collapse into one run)."""
    from uva.router import Router  # lazy: avoid an import cycle

    store = services.routines
    if store is None:
        return []
    now = now or utcnow()
    tz = services.tz
    ran = []
    router = None
    for routine in await store.due(now):
        late = now - routine.next_run_at > timedelta(minutes=10)
        router = router or Router(services)
        try:
            reply = await router.handle_text(routine.request)
            result = reply.text
        except Exception as exc:  # a broken routine must not block the others
            reply, result = None, f"falhou: {exc!r}"[:300]
        next_at = next_run(routine.model_copy(update={"last_run_at": now}), now, tz)
        await store.update(
            routine.id, last_run_at=now, next_run_at=next_at, runs=routine.runs + 1, last_result=result[:500]
        )
        ran.append(routine.id)
        if notify is not None:
            from uva.replies import Reply

            head = f"⏰ Rotina {routine.id}: {routine.request[:80]}"
            if late:
                head += f"\n(atrasada: era para {format_local(routine.next_run_at, tz, '%d/%m %H:%M')}; o PC estava desligado)"
            if reply is None:
                await notify(Reply(f"{head}\n{result}"))
            else:
                parts = [head, *(reply.parts or [reply.text])]
                out = Reply.from_parts(parts)
                out.buttons = reply.buttons
                await notify(out)
    return ran
