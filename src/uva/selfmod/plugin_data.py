"""The real, read-only ``ctx.data`` given to plugins.

Built per call from the bot's services. The services are captured in a
closure, not stored on an attribute, so plugin code can't reach them (the
static check forbids dunder/private attribute access like ``__closure__``).
Everything returned is a fresh plain dict/list: mutating it changes nothing.
"""

from __future__ import annotations

import asyncio
from typing import Any

from uva.clock import format_local, start_of_day, start_of_month, start_of_week, start_of_year
from uva.i18n import t
from uva.plugins.api import DataView
from uva.services import Services
from uva.storage.expenses import format_brl

MAX_ITEMS = 50
DATE = "%d/%m %H:%M"


def make_data_view(services: Services) -> DataView:
    tz = services.tz

    def when(dt) -> str | None:
        return format_local(dt, tz, DATE) if dt else None

    def goal_dict(goal) -> dict[str, Any]:
        return {
            "id": goal.id,
            "title": goal.title,
            "status": goal.status.value,
            "priority": goal.priority,
            "kind": goal.kind.value,
            "next_run": when(goal.next_run_at),
            "last_attempt": goal.last_attempt or t("selfmod.plugin_data.never"),
            "last_attempt_at": when(goal.last_attempt_at),
            "run_count": goal.run_count,
        }

    class _ReadOnlyData:
        __slots__ = ()

        async def working_memory(self, limit: int = 10) -> list[dict[str, Any]]:
            if services.memory is None:
                return []
            items = await services.memory.working_memory()
            return [
                {"key": i.key, "value": i.value, "category": i.category.value}
                for i in items[: min(limit, MAX_ITEMS)]
            ]

        async def memories(self, limit: int = 50) -> list[dict[str, Any]]:
            if services.memory is None:
                return []
            ranked = await services.memory.ranked()
            return [
                {"key": i.key, "value": i.value, "category": i.category.value, "score": round(s, 3)}
                for i, s in ranked[: min(limit, MAX_ITEMS)]
            ]

        async def memory(self, key: str) -> dict[str, Any] | None:
            if services.memory is None:
                return None
            item = await services.memory.get(key)
            return {"key": item.key, "value": item.value, "category": item.category.value} if item else None

        async def goals(self, status: str | None = None) -> list[dict[str, Any]]:
            if services.goals is None:
                return []
            goals = await services.goals.list()
            return [goal_dict(g) for g in goals if status is None or g.status.value == status][:MAX_ITEMS]

        async def goal(self, goal_id: str) -> dict[str, Any] | None:
            if services.goals is None:
                return None
            goal = await services.goals.get(goal_id)
            return goal_dict(goal) if goal else None

        async def recent_refs(self, limit: int = 10) -> list[dict[str, Any]]:
            if services.stm is None:
                return []
            session = await services.stm.session()
            return [
                {"ref": r.ref, "tipo": r.tipo, "rotulo": r.rotulo, "status": r.status, "quando": when(r.quando)}
                for r in session.refs[-min(limit, MAX_ITEMS):]
            ]

        async def expenses_summary(self, period: str = "mes") -> dict[str, Any]:
            starts = {
                "hoje": start_of_day, "semana": start_of_week, "mes": start_of_month,
                "ano": start_of_year,
            }
            if services.expenses is None:
                return {"period": period, "total": format_brl(0), "total_cents": 0, "by_category": []}
            since = starts[period](tz) if period in starts else None
            totals = await services.expenses.totals_by_category(since=since)
            total = sum(t for _, t in totals)
            return {
                "period": period,
                "total": format_brl(total),
                "total_cents": total,
                "by_category": [
                    {"category": c, "total": format_brl(t), "total_cents": t} for c, t in totals
                ],
            }

        async def recent_notes(self, limit: int = 10) -> list[dict[str, Any]]:
            if services.vault is None:
                return []
            notes = await asyncio.to_thread(services.vault.recent, min(limit, MAX_ITEMS))
            return [
                {"title": n.title, "path": n.path, "folder": n.path.split("/")[0], "changed": when(n.changed)}
                for n in notes
            ]

    return _ReadOnlyData()
