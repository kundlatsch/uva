"""Time helpers. Everything is stored in UTC; "today"/"this month" use TIMEZONE."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(UTC)


def get_tz(name: str) -> tzinfo:
    return ZoneInfo(name)


def start_of_day(tz: tzinfo, now: datetime | None = None) -> datetime:
    local = (now or utcnow()).astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def start_of_week(tz: tzinfo, now: datetime | None = None) -> datetime:
    """Monday 00:00 local time."""
    day = start_of_day(tz, now).astimezone(tz)
    return (day - timedelta(days=day.weekday())).astimezone(UTC)


def start_of_month(tz: tzinfo, now: datetime | None = None) -> datetime:
    local = (now or utcnow()).astimezone(tz)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def start_of_year(tz: tzinfo, now: datetime | None = None) -> datetime:
    local = (now or utcnow()).astimezone(tz)
    return local.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def format_local(dt: datetime, tz: tzinfo, fmt: str = "%d/%m %H:%M:%S") -> str:
    return dt.astimezone(tz).strftime(fmt)


def format_duration(delta: timedelta) -> str:
    """Compact human duration, e.g. '2d 3h 5m'."""
    total = int(delta.total_seconds())
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    parts = [f"{days}d"] if days else []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)
