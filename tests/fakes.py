"""In-memory stand-ins for external services, used across tests."""

from __future__ import annotations

from typing import Any

from uva.commands import build_registry
from uva.config import Settings
from uva.logging.events import Event, EventQuery
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


def make_services(**overrides: Any) -> Services:
    services = Services(
        settings=overrides.pop("settings", None) or make_settings(),
        registry=overrides.pop("registry", None) or build_registry(),
        events=overrides.pop("events", None) or InMemoryEventStore(),
    )
    for key, value in overrides.items():
        setattr(services, key, value)
    return services
