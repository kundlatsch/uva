"""Background task records and the queue interface used by the bot."""

from __future__ import annotations

import secrets
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field

from uva.clock import utcnow
from uva.i18n import t


# Background actions safe to run twice (re-delivered after a worker restart).
IDEMPOTENT_ACTIONS = frozenset({"search"})


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def finished(self) -> bool:
        return self in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED)


class TaskRecord(BaseModel):
    id: str = Field(default_factory=lambda: secrets.token_hex(3))
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    status: TaskStatus = TaskStatus.QUEUED
    trace_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    attempts: int = 0
    error: str | None = None


class TaskStore(Protocol):
    async def add(self, record: TaskRecord) -> None: ...

    async def get(self, task_id: str) -> TaskRecord | None: ...

    async def update(self, task_id: str, **fields: Any) -> None: ...

    async def recent(self, limit: int) -> list[TaskRecord]: ...


class TaskQueue(Protocol):
    store: TaskStore

    async def enqueue(self, action: str, params: dict[str, Any], trace_id: str | None) -> TaskRecord: ...

    async def cancel(self, task_id: str) -> bool: ...

    async def queue_size(self) -> int | None: ...


def describe_task(record: TaskRecord) -> str:
    label = t(f"tasks.queue.status.{record.status.value}")
    subject = record.params.get("query") or record.params.get("title") or ""
    subject = f' "{subject}"' if subject else ""
    return f"{record.id} {record.action}{subject}: {label}"
