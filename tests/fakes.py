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
from uva.goals.store import Goal, pick_due
from uva.memory.store import MemoryItem
from uva.search.base import SearchResponse, SearchResult
from uva.services import Services
from uva.tasks.queue import TaskRecord, TaskStatus


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


class InMemoryTaskStore:
    def __init__(self) -> None:
        self.records: dict[str, TaskRecord] = {}

    async def add(self, record: TaskRecord) -> None:
        self.records[record.id] = record

    async def get(self, task_id: str) -> TaskRecord | None:
        return self.records.get(task_id)

    async def update(self, task_id: str, **fields: Any) -> None:
        self.records[task_id] = self.records[task_id].model_copy(update=fields)

    async def recent(self, limit: int) -> list[TaskRecord]:
        return sorted(self.records.values(), key=lambda r: r.created_at, reverse=True)[:limit]


class FakeTaskQueue:
    """Records enqueued tasks instead of sending them to Celery."""

    def __init__(self) -> None:
        self.store = InMemoryTaskStore()
        self.sent: list[TaskRecord] = []

    async def enqueue(self, action: str, params: dict[str, Any], trace_id: str | None) -> TaskRecord:
        record = TaskRecord(action=action, params=params, trace_id=trace_id)
        await self.store.add(record)
        self.sent.append(record)
        return record

    async def cancel(self, task_id: str) -> bool:
        record = await self.store.get(task_id)
        if record is None or record.status.finished:
            return False
        await self.store.update(task_id, status=TaskStatus.CANCELLED)
        return True

    async def queue_size(self) -> int | None:
        return sum(1 for r in self.store.records.values() if r.status is TaskStatus.QUEUED)


class InMemoryMemoryStore:
    def __init__(self) -> None:
        self.items: dict[str, MemoryItem] = {}
        self.archived: list[tuple[MemoryItem, str]] = []
        self.working_set: tuple[list[str], datetime] | None = None

    async def get(self, key: str) -> MemoryItem | None:
        return self.items.get(key)

    async def upsert(self, item: MemoryItem) -> None:
        self.items[item.key] = item

    async def delete(self, key: str) -> bool:
        return self.items.pop(key, None) is not None

    async def all(self) -> list[MemoryItem]:
        return list(self.items.values())

    async def archive(self, items: list[MemoryItem], reason: str) -> None:
        for item in items:
            self.archived.append((item, reason))
            self.items.pop(item.key, None)

    async def load_working_set(self) -> tuple[list[str], datetime] | None:
        return self.working_set

    async def save_working_set(self, keys: list[str], computed_at: datetime) -> None:
        self.working_set = (keys, computed_at)


class InMemoryGoalStore:
    def __init__(self) -> None:
        self.goals: dict[str, Goal] = {}

    async def add(self, goal: Goal) -> None:
        self.goals[goal.id] = goal

    async def get(self, goal_id: str) -> Goal | None:
        return self.goals.get(goal_id)

    async def update(self, goal_id: str, **fields: Any) -> None:
        self.goals[goal_id] = self.goals[goal_id].model_copy(update=fields)

    async def delete(self, goal_id: str) -> bool:
        return self.goals.pop(goal_id, None) is not None

    async def list(self, status=None) -> list[Goal]:
        return [g for g in self.goals.values() if status is None or g.status == status]

    async def due(self, now: datetime) -> list[Goal]:
        return pick_due(list(self.goals.values()), now)


class FakeBusy:
    def __init__(self) -> None:
        self.markers: dict[str, int] = {}
        self.history: list[str] = []

    async def mark(self, reason: str, ttl_seconds: int) -> None:
        self.markers[reason] = ttl_seconds
        self.history.append(reason)

    async def clear(self, reason: str) -> None:
        self.markers.pop(reason, None)

    async def reasons(self) -> list[str]:
        return list(self.markers)

    async def is_busy(self) -> bool:
        return bool(self.markers)
