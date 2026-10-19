"""One-shot reminders: "daqui 20 min ...", "amanhã às 9 ...", "friday 2pm ...".

A reminder is a routine that runs once (``Every.ONCE`` with ``once_at``). A
plain one (``Routine.reminder``) sends its text with no LLM; one with a request
goes through the router like any routine. After it runs it is disabled and kept
for :data:`KEEP_DONE` (``/lembretes`` shows it), then the daily maintenance
deletes it.

:func:`parse_when` reads the time, in Portuguese or English, at the start or at
the end of the text. A weekday or a day of the month that already passed means
the next one.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

from uva.clock import format_local, get_tz, utcnow
from uva.i18n import t
from uva.text import normalize

KEEP_DONE = timedelta(days=7)
# A date without a time ("amanhã ligar pro João").
DEFAULT_TIME = time(9, 0)
# Moments of the day ("amanhã de manhã", "tomorrow evening", "sexta ao meio-dia").
PARTS_OF_DAY = {
    "manha": time(9, 0), "morning": time(9, 0),
    "tarde": time(15, 0), "afternoon": time(15, 0),
    "noite": time(20, 0), "evening": time(20, 0), "night": time(20, 0),
    "meio dia": time(12, 0), "noon": time(12, 0), "meia noite": time(0, 0), "midnight": time(0, 0),
}
_PART = (
    r"(?:(?:de|pela|a|ao|a|in\s+the|at|this)\s+)?"
    r"(?P<part>manha|tarde|noite|meio\s+dia|meia\s+noite|morning|afternoon|evening|night|noon|midnight)"
)

WEEKDAYS_PT = ("segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo")
WEEKDAYS_EN = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_WEEKDAY_PT = r"(?:segunda|terca|quarta|quinta|sexta)(?:\s+feira)?|sabado|domingo"
_WEEKDAY_EN = r"mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:rs?|rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?"

# Normalized text: "08:00" is "08 00", "9h30" stays, "às" is "as", "amanhã" is
# "amanha"; dates typed as 25/12 become "dt 25 12" (see _tokenize). A bare number
# is not a time ("amanhã 2 pães"): it needs "às"/"at", an "h"/am/pm or minutes.
_SUFFIX = r"h(?:oras?|rs?)?|am|pm"
_CLOCK = (
    rf"(?:(?:as|a|at|by)\s+(?P<h1>\d{{1,2}})(?:(?:\s+|h)(?P<m1>\d{{2}}))?\s*(?P<s1>{_SUFFIX})?"
    rf"|(?P<h2>\d{{1,2}})(?:\s+|h)(?P<m2>\d{{2}})\s*(?P<s2>{_SUFFIX})?"
    rf"|(?P<h3>\d{{1,2}})\s*(?P<s3>{_SUFFIX}))"
)
_TIME = rf"(?:\s+(?:{_CLOCK}|{_PART}))?"
_DAY = (
    r"(?P<rel>hoje|today|tonight|amanha|tomorrow|depois\s+de\s+amanha|day\s+after\s+tomorrow)"
    rf"|(?:(?:n[ao]s?|nest[ae]|next|this|on|(?:n[ao]\s+)?proxim[ao])\s+)?(?:(?P<wpt>{_WEEKDAY_PT})|(?P<wen>{_WEEKDAY_EN}))"
    r"|(?:(?:no\s+)?dia|(?:on\s+)?the|on)\s+(?P<dom>\d{1,2})(?:st|nd|rd|th)?"
    r"|(?P<dom2>\d{1,2})(?:st|nd|rd|th)"
    r"|(?:(?:em|no|dia|on)\s+)?dt\s+(?P<dd>\d{1,2})\s+(?P<mm>\d{1,2})(?:\s+y(?P<yy>\d{2,4}))?"
)
_END = r"(?=\s|$)"
_DATED = re.compile(rf"(?:{_DAY}){_TIME}{_END}")
_TIME_ONLY = re.compile(rf"(?:{_CLOCK}|{_PART}){_END}")
_UNITS = r"m|min|mins|minutos?|minutes?|h|hr|hrs|horas?|hours?|d|dias?|days?|semanas?|weeks?"
_AMOUNT = r"\d+(?:[.,]\d+)?|uma?|meia|an?|one|half\s+an?"
_RELATIVE = re.compile(
    rf"(?:daqui\s+(?:a\s+)?|em\s+|dentro\s+de\s+|in\s+|within\s+)(?P<n>{_AMOUNT})\s*(?P<u>{_UNITS}){_END}"
)
# Snooze: "/lembrete r1a2 adiar 30 min" (no "daqui"/"in" needed).
_DURATION = re.compile(rf"(?:\+\s*)?(?P<n>{_AMOUNT})\s*(?P<u>{_UNITS}){_END}")
_ISO = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})[T ](\d{1,2}):(\d{2})(?::\d{2})?\s*$")
_DATE_WORD = re.compile(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?[,.;]?$")
# What links the time and the text: "amanhã às 9 de ligar pro João".
_LEADING = re.compile(r"^(?:(?:de|pra|para|que|to|about|:|-|,)\s+)+", re.IGNORECASE)
_TRAILING = re.compile(r"[\s,]+(?:as|às|at|on|no|na|em|dia|in)$", re.IGNORECASE)
_NUMBERS = {"um": 1.0, "uma": 1.0, "a": 1.0, "an": 1.0, "one": 1.0, "meia": 0.5, "half a": 0.5, "half an": 0.5}


class WhenError(ValueError):
    """A time that was understood but can't be used (25h, 31/02)."""


@dataclass(frozen=True, slots=True)
class When:
    at: datetime  # UTC
    rest: str  # the text around the time, as typed


def _amount(raw: str) -> float:
    raw = " ".join(raw.split())
    return _NUMBERS[raw] if raw in _NUMBERS else float(raw.replace(",", "."))


def _delta(amount: float, unit: str) -> timedelta:
    if unit[0] == "m":
        return timedelta(minutes=amount)
    if unit[0] == "h":
        return timedelta(hours=amount)
    if unit[0] in "sw":
        return timedelta(weeks=amount)
    return timedelta(days=amount)


def _clock(m: re.Match[str]) -> time | None:
    """The time of day in a match (None when it has none)."""
    groups = m.groupdict()
    if groups.get("part"):
        return PARTS_OF_DAY[" ".join(groups["part"].split())]
    for n in "123":
        if groups.get(f"h{n}") is not None:
            hour, minute, suffix = groups[f"h{n}"], groups.get(f"m{n}"), groups.get(f"s{n}")
            break
    else:
        return None
    h, mi = int(hour), int(minute or 0)
    if suffix in ("am", "pm"):
        if not 1 <= h <= 12:
            raise WhenError(t("routines.errors.bad_time"))
        h = h % 12 + (12 if suffix == "pm" else 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise WhenError(t("routines.errors.bad_time"))
    return time(h, mi)


def _month_day(today: date, day: int, at: time, now_local: datetime, tz: tzinfo) -> date:
    """Day ``day`` of this month, or of the next month that has it once it passed."""
    if not 1 <= day <= 31:
        raise WhenError(t("reminders.errors.bad_date"))
    year, month = today.year, today.month
    for _ in range(13):
        if day <= calendar.monthrange(year, month)[1]:
            candidate = date(year, month, day)
            if datetime.combine(candidate, at, tzinfo=tz) > now_local:
                return candidate
        month += 1
        if month > 12:
            year, month = year + 1, 1
    raise WhenError(t("reminders.errors.bad_date"))


def _dated(m: re.Match[str], local: datetime, tz: tzinfo) -> datetime | None:
    today = local.date()
    groups = m.groupdict()
    at = _clock(m)
    if (rel := groups.get("rel")) is not None:
        rel = " ".join(rel.split())
        if rel == "tonight" and at is None:
            at = PARTS_OF_DAY["night"]
        at = at or DEFAULT_TIME
        offset = 0 if rel in ("hoje", "today", "tonight") else 1 if rel in ("amanha", "tomorrow") else 2
        day = today + timedelta(days=offset)
    elif groups.get("wpt") or groups.get("wen"):
        at = at or DEFAULT_TIME
        if groups.get("wpt"):
            weekday = WEEKDAYS_PT.index(groups["wpt"].split()[0])
        else:
            weekday = next(i for i, name in enumerate(WEEKDAYS_EN) if name.startswith(groups["wen"][:3]))
        day = today + timedelta(days=(weekday - today.weekday()) % 7)
        if datetime.combine(day, at, tzinfo=tz) <= local:
            day += timedelta(days=7)
    elif dom := groups.get("dom") or groups.get("dom2"):
        at = at or DEFAULT_TIME
        day = _month_day(today, int(dom), at, local, tz)
    elif groups.get("dd"):
        at = at or DEFAULT_TIME
        d, mo = int(groups["dd"]), int(groups["mm"])
        year = int(groups["yy"]) if groups.get("yy") else today.year
        year += 2000 if year < 100 else 0
        try:
            day = date(year, mo, d)
            if not groups.get("yy") and datetime.combine(day, at, tzinfo=tz) <= local:
                day = date(year + 1, mo, d)
        except ValueError as exc:
            raise WhenError(t("reminders.errors.bad_date")) from exc
    else:
        return None
    return datetime.combine(day, at, tzinfo=tz)


def _matches(norm: str, local: datetime, tz: tzinfo, *, snooze: bool) -> list[tuple[int, datetime]]:
    """(normalized words used, local time) for each reading of the start of ``norm``."""
    found: list[tuple[re.Match[str], datetime]] = []
    if m := _RELATIVE.match(norm):
        found.append((m, local + _delta(_amount(m.group("n")), m.group("u"))))
    if snooze and (m := _DURATION.match(norm)):
        found.append((m, local + _delta(_amount(m.group("n")), m.group("u"))))
    if (m := _DATED.match(norm)) and (at := _dated(m, local, tz)) is not None:
        found.append((m, at))
    if (m := _TIME_ONLY.match(norm)) and (clock := _clock(m)) is not None:
        at = datetime.combine(local.date(), clock, tzinfo=tz)
        found.append((m, at if at > local else at + timedelta(days=1)))
    found.sort(key=lambda p: -p[0].end())
    return [(len(norm[: m.end()].split()), at) for m, at in found]


def _tokenize(text: str) -> list[tuple[str, list[str]]]:
    """Each raw word with its normalized words; 25/12 becomes "dt 25 12"."""
    out = []
    for word in text.split():
        if m := _DATE_WORD.match(word):
            out.append((word, ["dt", m[1], m[2], *([f"y{m[3]}"] if m[3] else [])]))
        else:
            out.append((word, normalize(word).split()))
    return out


def _raw(tokens: list[tuple[str, list[str]]], start: int, end: int | None = None) -> str:
    """The raw words covering normalized words [start, end)."""
    out, seen = [], 0
    for word, parts in tokens:
        if seen >= start and (end is None or seen < end):
            out.append(word)
        seen += len(parts)
    return " ".join(out)


def parse_when(
    text: str, now: datetime | None = None, tz: tzinfo | None = None, *, snooze: bool = False
) -> When | None:
    """The time at the start ("amanhã 9h ligar pro João") or at the end
    ("ligar pro João amanhã 9h") of ``text``; None when there is none.
    ``snooze`` also takes a bare duration ("30 min", "2h"). Raises WhenError
    for a time that can't be (25h, 31/02)."""
    now = now or utcnow()
    tz = tz or get_tz("America/Sao_Paulo")
    local = now.astimezone(tz)
    if m := _ISO.match(text):
        try:
            at = datetime(*(int(g) for g in m.groups()), tzinfo=tz)
        except ValueError as exc:
            raise WhenError(t("reminders.errors.bad_date")) from exc
        return When(at.astimezone(now.tzinfo), "")
    tokens = _tokenize(text)
    words = [w for _, parts in tokens for w in parts]
    if not words:
        return None
    norm = " ".join(words)
    if readings := _matches(norm, local, tz, snooze=snooze):
        used, at = readings[0]
        rest = _LEADING.sub("", _raw(tokens, used)).strip()
        return When(at.astimezone(now.tzinfo), rest)
    # At the end: the longest tail of whole words that reads as a time.
    for start in range(1, len(words)):
        tail = " ".join(words[start:])
        for used, at in _matches(tail, local, tz, snooze=snooze):
            if used == len(words) - start:
                rest = _TRAILING.sub("", _raw(tokens, 0, start)).strip()
                return When(at.astimezone(now.tzinfo), rest)
    return None


def describe_at(at: datetime, tz: tzinfo, now: datetime | None = None) -> str:
    """'hoje 18:00', 'amanhã 09:00' or '25/12 08:00'."""
    local, today = at.astimezone(tz), (now or utcnow()).astimezone(tz).date()
    if local.date() == today:
        return t("reminders.when.today", time=local.strftime("%H:%M"))
    if local.date() == today + timedelta(days=1):
        return t("reminders.when.tomorrow", time=local.strftime("%H:%M"))
    return format_local(at, tz, "%d/%m %H:%M")


# --- reminders as routines ----------------------------------------------------


def notice_buttons(routine_id: str) -> list:
    from uva.replies import Button

    return [
        Button(t("reminders.button.done"), f"rmdok:{routine_id}"),
        Button(t("reminders.button.hour"), f"rmd1h:{routine_id}"),
        Button(t("reminders.button.tomorrow"), f"rmdtm:{routine_id}"),
    ]


async def create_reminder(
    services, when: str, text: str = "", request: str = "", now: datetime | None = None
) -> str:
    """Shared by /lembrete (``when`` holds the text too) and reminder_create.
    A ``request`` runs through the router at that time; otherwise ``text`` is
    sent as is."""
    from uva.routines import Every, Routine

    if services.routines is None:
        return t("commands.routines.not_configured")
    now = now or utcnow()
    try:
        found = parse_when(when.strip(), now, services.tz)
    except WhenError as exc:
        return t("reminders.bad_when", error=exc)
    if found is None:
        return t("reminders.usage")
    text = (text or found.rest).strip()
    request = request.strip()
    if found.at <= now:
        return t("reminders.past", when=format_local(found.at, services.tz, "%d/%m %H:%M"))
    if not (text or request):
        return t("reminders.usage")
    routine = Routine(
        request=request or text, every=Every.ONCE, once_at=found.at, next_run_at=found.at,
        reminder="" if request else text,
    )
    await services.routines.add(routine)
    key = "reminders.created_request" if request else "reminders.created"
    return t(key, id=routine.id, when=describe_at(found.at, services.tz, now), text=routine.request)


async def snooze(services, routine_id: str, until: datetime) -> str:
    store = services.routines
    routine = await store.get(routine_id) if store is not None else None
    if routine is None or not routine.is_reminder:
        return t("reminders.not_found", id=routine_id)
    await store.update(routine.id, once_at=until, next_run_at=until, enabled=True, last_status="")
    return t("reminders.snoozed", id=routine.id, when=describe_at(until, services.tz), text=routine.request[:80])


def tomorrow_same_time(routine, now: datetime, tz: tzinfo) -> datetime:
    """Tomorrow at the time the reminder was set for."""
    clock = (routine.once_at or now).astimezone(tz).timetz().replace(tzinfo=None)
    day = now.astimezone(tz).date() + timedelta(days=1)
    return datetime.combine(day, clock, tzinfo=tz).astimezone(now.tzinfo)


async def handle_button(services, verb: str, routine_id: str, now: datetime | None = None) -> str:
    """✅ Feito / ⏰ +1h / ⏰ amanhã under a reminder's notice."""
    now = now or utcnow()
    store = services.routines
    routine = await store.get(routine_id) if store is not None else None
    if routine is None or not routine.is_reminder:
        return t("reminders.not_found", id=routine_id)
    if verb == "rmdok":
        await store.update(routine.id, last_status="done")
        return t("reminders.done", text=routine.request[:80])
    until = now + timedelta(hours=1) if verb == "rmd1h" else tomorrow_same_time(routine, now, services.tz)
    return await snooze(services, routine.id, until)


async def cancel(services, routine_id: str) -> str:
    store = services.routines
    routine = await store.get(routine_id) if store is not None else None
    if routine is None or not routine.is_reminder:
        return t("reminders.not_found", id=routine_id)
    await store.delete(routine.id)
    return t("reminders.cancelled", id=routine.id, text=routine.request[:80])


def describe_reminder(routine, tz: tzinfo, now: datetime | None = None) -> str:
    at = routine.once_at or routine.next_run_at
    mark = "⏰" if routine.enabled else ("✅" if routine.last_status in ("ok", "done", "silent") else "❌")
    kind = "" if routine.reminder else " " + t("reminders.list.request")
    return f"{mark} {routine.id} · {describe_at(at, tz, now)} · {routine.request[:120]}{kind}"


async def listing(services, now: datetime | None = None) -> str:
    now = now or utcnow()
    reminders = [r for r in await services.routines.all() if r.is_reminder]
    pending = sorted((r for r in reminders if r.enabled), key=lambda r: r.next_run_at)
    recent = sorted(
        (r for r in reminders if not r.enabled and (r.last_run_at or r.created_at) >= now - KEEP_DONE),
        key=lambda r: r.last_run_at or r.created_at, reverse=True,
    )
    if not pending and not recent:
        return t("reminders.list.none")
    lines = [t("reminders.list.header", count=len(pending))]
    lines += [describe_reminder(r, services.tz, now) for r in pending] or [t("reminders.list.no_pending")]
    if recent:
        lines += ["", t("reminders.list.recent")] + [describe_reminder(r, services.tz, now) for r in recent]
    return "\n".join(lines)


async def purge(store, now: datetime | None = None) -> int:
    """Reminders that ran more than a week ago (daily maintenance)."""
    if store is None:
        return 0
    cutoff = (now or utcnow()) - KEEP_DONE
    gone = 0
    for routine in await store.all():
        if routine.is_reminder and not routine.enabled and (routine.last_run_at or routine.created_at) < cutoff:
            gone += await store.delete(routine.id)
    return gone
