"""In-memory stand-ins for external services, used across tests."""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Any

from uva.actions import build_action_registry
from uva.commands import build_registry
from uva.config import Settings
from uva.llm.base import LLMMessage, LLMResponse
from uva.llm.budget import Budget, Scope, UsageRecord
from uva.llm.client import LLMClient
from uva.llm.pricing import PriceTable
from uva.logging.events import Event, EventQuery
from uva.search.base import SearchResponse, SearchResult
from uva.services import Services


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "telegram_bot_token": "123:abc",
        "allowed_telegram_user_id": 1001,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class InMemoryEventStore:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def add(self, event: Event) -> None:
        self.events.append(event)

    async def find(self, query: EventQuery) -> list[Event]:
        found = [
            e
            for e in self.events
            if (query.trace_id is None or e.trace_id == query.trace_id)
            and (query.types is None or e.type in query.types)
            and (query.since is None or e.ts >= query.since)
            and (query.until is None or e.ts < query.until)
        ]
        return sorted(found, key=lambda e: e.ts)[-query.limit :]

    def of_type(self, event_type) -> list[Event]:
        return [e for e in self.events if e.type == event_type]


class InMemoryUsageStore:
    def __init__(self) -> None:
        self.records: list[UsageRecord] = []

    async def add(self, record: UsageRecord) -> None:
        self.records.append(record)

    async def total_cost(self, since: datetime, scope: Scope | None = None) -> float:
        return sum(
            r.cost_usd
            for r in self.records
            if r.ts >= since and (scope is None or r.scope == scope)
        )


class FakeProvider:
    """Returns scripted replies in order and records every call."""

    name = "fake"

    def __init__(self, *replies: str) -> None:
        self.replies = deque(replies)
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        model: str,
        system: str | None = None,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 4000,
    ) -> LLMResponse:
        self.calls.append(
            {"messages": list(messages), "model": model, "system": system, "schema": json_schema}
        )
        return LLMResponse(text=self.replies.popleft(), model=model, input_tokens=100, output_tokens=50)


class FakeSearch:
    def __init__(self, name: str, *results: SearchResult, summary: str | None = None) -> None:
        self.name = name
        self.results = list(results)
        self.summary = summary
        self.queries: list[str] = []

    async def search(self, query: str, max_results: int) -> SearchResponse:
        self.queries.append(query)
        return SearchResponse(results=self.results[:max_results], summary=self.summary)


def make_llm(provider: FakeProvider, usage: InMemoryUsageStore | None = None) -> LLMClient:
    return LLMClient(
        provider,
        router_model="router-model",
        heavy_model="heavy-model",
        prices=PriceTable({"router-model": (1.0, 2.0), "heavy-model": (10.0, 20.0)}, (5.0, 25.0)),
        usage=usage,
    )


def make_services(**overrides: Any) -> Services:
    services = Services(
        settings=overrides.pop("settings", None) or make_settings(),
        registry=overrides.pop("registry", None) or build_registry(),
        actions=overrides.pop("actions", None) or build_action_registry(),
        events=overrides.pop("events", None) or InMemoryEventStore(),
    )
    for key, value in overrides.items():
        setattr(services, key, value)
    return services


def with_llm(services: Services, *replies: str) -> tuple[FakeProvider, InMemoryUsageStore]:
    """Attach a scripted LLM, a usage store and a budget to ``services``."""
    provider = FakeProvider(*replies)
    usage = InMemoryUsageStore()
    services.llm = make_llm(provider, usage)
    services.budget = Budget(usage, services.settings, services.tz)
    return provider, usage
