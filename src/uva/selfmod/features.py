"""Feature requests (/feature) and their records."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field

from uva.clock import utcnow


class FeatureStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    MERGED = "merged"
    FAILED = "failed"
    REVERTED = "reverted"


class FeatureRecord(BaseModel):
    id: str = Field(default_factory=lambda: secrets.token_hex(3))
    description: str
    status: FeatureStatus = FeatureStatus.QUEUED
    trace_id: str | None = None
    plugin: str | None = None  # module name
    command: str | None = None
    summary: str = ""
    usage: str = ""
    attempts: int = 0
    tests: str = ""  # pytest summary line
    commit: str | None = None
    cost_usd: float = 0.0
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None


class FeatureStore(Protocol):
    async def add(self, record: FeatureRecord) -> None: ...

    async def get(self, feature_id: str) -> FeatureRecord | None: ...

    async def update(self, feature_id: str, **fields: Any) -> None: ...

    async def recent(self, limit: int) -> list[FeatureRecord]: ...


Enqueue = Callable[[str], Awaitable[None]]


class FeatureService:
    def __init__(self, store: FeatureStore, enqueue: Enqueue | None) -> None:
        self.store = store
        self.enqueue = enqueue

    async def request(self, description: str, trace_id: str | None) -> FeatureRecord:
        if self.enqueue is None:
            raise RuntimeError("a fila de tarefas não está configurada")
        record = FeatureRecord(description=description.strip(), trace_id=trace_id)
        await self.store.add(record)
        await self.enqueue(record.id)
        return record


STATUS_LABEL = {
    FeatureStatus.QUEUED: "⏳ na fila",
    FeatureStatus.RUNNING: "🔨 construindo",
    FeatureStatus.MERGED: "✅ pronta",
    FeatureStatus.FAILED: "❌ falhou",
    FeatureStatus.REVERTED: "↩️ revertida",
}


def describe_feature(record: FeatureRecord) -> str:
    head = f"{record.id} {STATUS_LABEL[record.status]}"
    if record.command:
        head += f" /{record.command}"
    lines = [head, f"   {record.description[:120]}"]
    details = []
    if record.attempts:
        details.append(f"{record.attempts} tentativa(s)")
    if record.cost_usd:
        details.append(f"US$ {record.cost_usd:.3f}")
    if record.commit:
        details.append(f"commit {record.commit[:7]}")
    if details:
        lines.append("   " + " · ".join(details))
    if record.error and record.status is not FeatureStatus.MERGED:
        lines.append(f"   motivo: {record.error[:200]}")
    return "\n".join(lines)
