"""Traceable events: every step of handling a message is recorded with a trace_id.

Events go to the console (JSON, via structlog) and to an :class:`EventStore`
(Mongo in production). Code anywhere in the app calls :func:`emit`; the trace
id comes from a context variable set by :func:`trace`.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

import structlog
from pydantic import BaseModel, Field
from pydantic_core import to_jsonable_python

from uva.clock import utcnow
from uva.logging import get_logger

log = get_logger("uva.events")

MAX_STRING_LENGTH = 4000


class EventType(StrEnum):
    MESSAGE_RECEIVED = "message_received"
    ROUTE = "route"  # which routing tier resolved the message
    COMMAND = "command"
    ACTION = "action"
    LLM_CALL = "llm_call"
    TASK_ENQUEUED = "task_enqueued"
    TASK_FINISHED = "task_finished"
    GOAL_RUN = "goal_run"
    MEMORY = "memory"
    BUDGET = "budget"
    SELF_EDIT = "self_edit"  # the bot changed one of its own files
    SELFCODE = "selfcode"  # the bot wrote/merged/reverted its own code
    REPLY = "reply"
    ERROR = "error"


class Event(BaseModel):
    trace_id: str
    type: EventType
    ts: datetime = Field(default_factory=utcnow)
    data: dict[str, Any] = Field(default_factory=dict)


class EventQuery(BaseModel):
    since: datetime | None = None
    until: datetime | None = None
    types: set[EventType] | None = None
    trace_id: str | None = None
    limit: int = 1000


class EventStore(Protocol):
    async def add(self, event: Event) -> None: ...

    async def find(self, query: EventQuery) -> list[Event]:
        """Matching events, oldest first."""
        ...


_current_trace: ContextVar[str | None] = ContextVar("uva_trace_id", default=None)
_store: EventStore | None = None


def set_store(store: EventStore | None) -> None:
    global _store
    _store = store


def new_trace_id() -> str:
    # Short enough to type in "/log trace <id>".
    return secrets.token_hex(4)


def current_trace_id() -> str | None:
    return _current_trace.get()


@contextmanager
def trace(trace_id: str | None = None) -> Iterator[str]:
    """Run a block under a trace id (new one unless given)."""
    trace_id = trace_id or new_trace_id()
    token = _current_trace.set(trace_id)
    with structlog.contextvars.bound_contextvars(trace_id=trace_id):
        try:
            yield trace_id
        finally:
            _current_trace.reset(token)


async def emit(event_type: EventType, **data: Any) -> Event:
    """Record an event for the current trace (console + store)."""
    trace_id = current_trace_id() or "untraced"
    event = Event(trace_id=trace_id, type=event_type, data=_sanitize(data))

    level = "error" if event_type is EventType.ERROR else "info"
    getattr(log, level)(event_type.value, **event.data)

    if _store is not None:
        try:
            await _store.add(event)
        except Exception as exc:  # the event log must never break the bot
            log.warning("event_store_unavailable", error=str(exc))
    return event


def _sanitize(data: dict[str, Any]) -> dict[str, Any]:
    """JSON/BSON-safe copy with long strings truncated."""
    clean = to_jsonable_python(data, fallback=str)
    return _truncate(clean)


def _truncate(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_STRING_LENGTH:
        return value[:MAX_STRING_LENGTH] + "…"
    if isinstance(value, dict):
        return {k: _truncate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate(v) for v in value]
    return value
