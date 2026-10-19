"""Memory items and their storage interface."""

from __future__ import annotations

import math
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field

from uva.clock import utcnow
from uva.text import normalize


class MemoryCategory(StrEnum):
    PERFIL = "perfil"
    PREFERENCIA = "preferencia"
    FATO = "fato"
    CONTEXTO = "contexto"


class MemoryItem(BaseModel):
    key: str
    value: str
    category: MemoryCategory = MemoryCategory.FATO
    relevance: float = Field(default=0.5, ge=0, le=1)
    created_at: datetime = Field(default_factory=utcnow)
    last_used_at: datetime = Field(default_factory=utcnow)
    use_count: int = 0
    expires_at: datetime | None = None  # removed by a Mongo TTL index
    source: str | None = None  # trace_id that created it
    # Who it came from (uva/provenance.py): owner (/lembrar), agent (the LLM), or
    # external (kept after reading outside content, with his confirmation).
    origin: str = "owner"

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at is not None and self.expires_at <= now


def normalize_key(key: str) -> str:
    """'Cidade Natal' -> 'cidade_natal' so the same fact maps to one key."""
    return normalize(key).replace(" ", "_")


def score(item: MemoryItem, now: datetime, half_life_days: float) -> float:
    """relevance x recency x frequency, all in 0-1.

    recency halves every `half_life_days` without use; frequency saturates
    (1 use = 0.18, 5 uses = 0.63, 15 uses = 0.95).
    """
    idle_days = max(0.0, (now - item.last_used_at).total_seconds() / 86400)
    recency = 0.5 ** (idle_days / half_life_days)
    frequency = 1 - math.exp(-(item.use_count + 1) / 5)
    return item.relevance * recency * frequency


class MemoryStore(Protocol):
    async def get(self, key: str) -> MemoryItem | None: ...

    async def upsert(self, item: MemoryItem) -> None: ...

    async def delete(self, key: str) -> bool: ...

    async def all(self) -> list[MemoryItem]: ...

    async def touch(self, keys: list[str], at: datetime) -> None:
        """Mark items as used (last_used_at = at, use_count + 1)."""
        ...

    async def archive(self, items: list[MemoryItem], reason: str) -> None:
        """Move items to the archive collection (removing them from memory)."""
        ...

    async def load_working_set(self) -> tuple[list[str], datetime] | None: ...

    async def save_working_set(self, keys: list[str], computed_at: datetime) -> None: ...
