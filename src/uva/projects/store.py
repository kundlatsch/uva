"""Projects (/projeto): new, separate code projects built by Claude Code."""

from __future__ import annotations

import secrets
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field

from uva.clock import utcnow


class ProjectStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"  # asked by /cancelar; the runner stops Claude Code
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE = (ProjectStatus.QUEUED, ProjectStatus.RUNNING, ProjectStatus.CANCELLING)


class ProjectRecord(BaseModel):
    id: str = Field(default_factory=lambda: "p" + secrets.token_hex(3))
    description: str
    slug: str
    status: ProjectStatus = ProjectStatus.QUEUED
    trace_id: str | None = None
    summary: str = ""
    error: str | None = None
    cost_usd: float = 0.0
    turns: int = 0
    commits: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ProjectStore(Protocol):
    async def add(self, record: ProjectRecord) -> None: ...

    async def get(self, project_id: str) -> ProjectRecord | None: ...

    async def update(self, project_id: str, **fields: Any) -> None: ...

    async def recent(self, limit: int) -> list[ProjectRecord]: ...

    async def cost_since(self, since: datetime) -> float: ...


STATUS_LABEL = {
    ProjectStatus.QUEUED: "⏳ na fila",
    ProjectStatus.RUNNING: "🔨 construindo",
    ProjectStatus.CANCELLING: "🛑 cancelando",
    ProjectStatus.DONE: "✅ pronto",
    ProjectStatus.FAILED: "❌ falhou",
    ProjectStatus.CANCELLED: "🚫 cancelado",
}
