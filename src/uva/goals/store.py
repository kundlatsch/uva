"""Goals the bot works on by itself, and their storage interface."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field

from uva.clock import utcnow

MAX_RESULTS_KEPT = 10


class GoalKind(StrEnum):
    RECORRENTE = "recorrente"  # runs again every min_interval
    UNICO = "unico"  # done after the LLM says it is complete


class GoalStatus(StrEnum):
    ATIVO = "ativo"
    PAUSADO = "pausado"
    CONCLUIDO = "concluido"


class GoalResult(BaseModel):
    at: datetime = Field(default_factory=utcnow)
    summary: str
    note: str | None = None  # vault path where the result was written
    ok: bool = True


class Goal(BaseModel):
    id: str = Field(default_factory=lambda: secrets.token_hex(3))
    title: str
    description: str = ""
    kind: GoalKind = GoalKind.RECORRENTE
    priority: int = Field(default=3, ge=1, le=5)  # 5 = most important
    status: GoalStatus = GoalStatus.ATIVO
    min_interval_hours: float = Field(default=24, gt=0)
    created_at: datetime = Field(default_factory=utcnow)
    last_run_at: datetime | None = None
    next_run_at: datetime = Field(default_factory=utcnow)
    run_count: int = 0
    results: list[GoalResult] = Field(default_factory=list)
    # Last time the loop picked this goal, whatever happened ("ok", "falhou",
    # "pulado: ocupado"...), so a goal that never runs can be debugged.
    last_attempt_at: datetime | None = None
    last_attempt: str | None = None
    # Set when the goal needs a capability goal runs don't have (a new command).
    needs_feature: str | None = None

    @property
    def min_interval(self) -> timedelta:
        return timedelta(hours=self.min_interval_hours)


class GoalStore(Protocol):
    async def add(self, goal: Goal) -> None: ...

    async def get(self, goal_id: str) -> Goal | None: ...

    async def update(self, goal_id: str, **fields: Any) -> None: ...

    async def delete(self, goal_id: str) -> bool: ...

    async def list(self, status: GoalStatus | None = None) -> list[Goal]: ...

    async def due(self, now: datetime) -> list[Goal]:
        """Active goals whose next_run_at has passed, most important first."""
        ...


def pick_due(goals: list[Goal], now: datetime) -> list[Goal]:
    """Shared ordering for stores: priority desc, then the most overdue."""
    due = [g for g in goals if g.status is GoalStatus.ATIVO and g.next_run_at <= now]
    return sorted(due, key=lambda g: (-g.priority, g.next_run_at))
