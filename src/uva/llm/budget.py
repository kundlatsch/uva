"""LLM spend tracking and budget enforcement.

Every call is recorded in a usage store (Mongo collection ``llm_usage``).
Before calling the LLM, callers ask :meth:`Budget.check`:
  * interactive calls stop when the daily or monthly budget is used up;
  * proactive calls (goal loop) also stop at PROACTIVE_DAILY_BUDGET_USD.
Everything resets when the day/month turns in TIMEZONE.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, tzinfo
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field

from uva.clock import start_of_day, start_of_month, utcnow
from uva.config import Settings


class Scope(StrEnum):
    INTERACTIVE = "interactive"
    PROACTIVE = "proactive"
    SELFCODE = "selfcode"  # has its own daily budget, outside the general one


class UsageRecord(BaseModel):
    ts: datetime = Field(default_factory=utcnow)
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    scope: Scope
    purpose: str
    trace_id: str | None = None


class ExtraStore(Protocol):
    """Extra daily allowance granted with /liberar, per local day ("2026-10-11")."""

    async def get(self, day: str) -> float: ...

    async def add(self, day: str, amount: float) -> float:
        """Add to the day's extra; returns the new total."""
        ...


class UsageStore(Protocol):
    async def add(self, record: UsageRecord) -> None: ...

    async def total_cost(self, since: datetime, scope: Scope | None = None) -> float: ...


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    allowed: bool
    reason: str = ""  # user-facing explanation when not allowed


@dataclass(frozen=True, slots=True)
class Spend:
    today: float  # interactive + proactive (self-coding has its own budget)
    month: float  # everything
    proactive_today: float
    selfcode_today: float = 0.0
    extra_today: float = 0.0  # granted with /liberar, valid until local midnight

    def daily_limit(self, settings: Settings) -> float:
        return settings.llm_daily_budget_usd + self.extra_today


class Budget:
    def __init__(
        self, store: UsageStore, settings: Settings, tz: tzinfo, extras: ExtraStore | None = None
    ) -> None:
        self.store = store
        self.settings = settings
        self.tz = tz
        self.extras = extras

    def day_key(self, now: datetime | None = None) -> str:
        return (now or utcnow()).astimezone(self.tz).strftime("%Y-%m-%d")

    async def grant_extra(self, amount: float, now: datetime | None = None) -> float:
        """Allow ``amount`` USD more today (the monthly budget still applies)."""
        if self.extras is None:
            raise RuntimeError("liberação de orçamento não está configurada")
        return await self.extras.add(self.day_key(now), amount)

    async def spend(self, now: datetime | None = None) -> Spend:
        day = start_of_day(self.tz, now)
        selfcode = await self.store.total_cost(day, Scope.SELFCODE)
        return Spend(
            today=await self.store.total_cost(day) - selfcode,
            month=await self.store.total_cost(start_of_month(self.tz, now)),
            proactive_today=await self.store.total_cost(day, Scope.PROACTIVE),
            selfcode_today=selfcode,
            extra_today=await self.extras.get(self.day_key(now)) if self.extras else 0.0,
        )

    async def check(self, scope: Scope, now: datetime | None = None) -> BudgetStatus:
        s = self.settings
        try:
            spend = await self.spend(now)
        except Exception:
            # Without knowing the spend we can't promise to stay in budget.
            return BudgetStatus(False, "não consegui verificar o gasto com a API (Mongo fora?)")

        if spend.month >= s.llm_monthly_budget_usd:
            return BudgetStatus(
                False,
                f"orçamento mensal de LLM atingido (US$ {spend.month:.2f} de "
                f"{s.llm_monthly_budget_usd:.2f}); volta no próximo mês",
            )
        if scope is Scope.SELFCODE:
            if spend.selfcode_today >= s.selfcode_daily_budget_usd:
                return BudgetStatus(
                    False,
                    f"orçamento diário de auto-código atingido (US$ {spend.selfcode_today:.2f} de "
                    f"{s.selfcode_daily_budget_usd:.2f}); volta amanhã",
                )
            return BudgetStatus(True)
        if spend.today >= spend.daily_limit(s):
            extra = f", com US$ {spend.extra_today:.2f} liberados" if spend.extra_today else ""
            return BudgetStatus(
                False,
                f"orçamento diário de LLM atingido (US$ {spend.today:.2f} de "
                f"{spend.daily_limit(s):.2f}{extra}); volta amanhã ou use /liberar",
            )
        if scope is Scope.PROACTIVE and spend.proactive_today >= s.proactive_daily_budget_usd:
            return BudgetStatus(False, "orçamento diário do modo proativo atingido")
        return BudgetStatus(True)
