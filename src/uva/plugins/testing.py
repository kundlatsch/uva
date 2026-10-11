"""Helpers for plugin tests (keep plugin tests plain synchronous functions)."""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime
from typing import Any

from uva.plugins.api import DataView, Handler, NoData, PluginContext


class StaticData(NoData):
    """Fake ``ctx.data`` for tests: pass the lists/dicts each method returns.

    Example: ``StaticData(goals=[{"id": "a1", "title": "Ler", "status": "ativo", ...}])``
    """

    def __init__(self, **values: Any) -> None:
        self.values = values

    async def working_memory(self, limit: int = 10):
        return self.values.get("working_memory", [])[:limit]

    async def memories(self, limit: int = 50):
        return self.values.get("memories", [])[:limit]

    async def memory(self, key: str):
        return next((m for m in self.values.get("memories", []) if m.get("key") == key), None)

    async def goals(self, status: str | None = None):
        goals = self.values.get("goals", [])
        return [g for g in goals if status is None or g.get("status") == status]

    async def goal(self, goal_id: str):
        return next((g for g in self.values.get("goals", []) if g.get("id") == goal_id), None)

    async def recent_refs(self, limit: int = 10):
        return self.values.get("recent_refs", [])[-limit:]

    async def expenses_summary(self, period: str = "mes"):
        return self.values.get("expenses_summary", await super().expenses_summary(period))

    async def recent_notes(self, limit: int = 10):
        return self.values.get("recent_notes", [])[:limit]


def run(
    handler: Handler, *args: str, now: datetime | None = None, data: DataView | None = None
) -> str:
    """Call a plugin handler as if the user typed ``/<command> <args...>``."""
    context = PluginContext(args=list(args), now=now or datetime.now(), data=data or NoData())
    result = handler(context)
    if inspect.isawaitable(result):
        result = asyncio.run(_await(result))
    return str(result)


async def _await(awaitable):
    return await awaitable
