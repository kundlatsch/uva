"""history_search: the conversation log, for questions about old conversations."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from pydantic import BaseModel, Field

from uva.actions.registry import ActionContext, ActionRegistry, ActionResult
from uva.i18n import t
from uva.recall import render_turns
from uva.text import normalize

LIMIT = 15


class HistorySearchParams(BaseModel):
    query: str = Field(default="", description="Words to look for (empty: everything in the period)")
    since: str = Field(default="", description="Start date YYYY-MM-DD (optional)")
    until: str = Field(default="", description="End date YYYY-MM-DD, inclusive (optional)")


def _day(value: str, tz, *, end: bool = False) -> datetime | None:
    if not value.strip():
        return None
    day = date.fromisoformat(value.strip()[:10])
    if end:
        day += timedelta(days=1)
    return datetime.combine(day, time(), tz)


def register(registry: ActionRegistry) -> None:
    @registry.action(
        "history_search",
        "Searches every past conversation with the user (the conversation log, beyond the last hours)",
        HistorySearchParams,
        proactive_safe=True,
        lookup=True,
    )
    async def history_search(params: HistorySearchParams, ctx: ActionContext) -> ActionResult:
        index = ctx.services.index
        if index is None:
            return ActionResult(text=t("actions.history.unavailable"), data={"ok": False})
        tz = ctx.services.tz
        try:
            since, until = _day(params.since, tz), _day(params.until, tz, end=True)
        except ValueError:
            return ActionResult(text=t("actions.history.bad_date"), data={"ok": False})
        words = normalize(params.query).split()
        if words:
            turns = await index.search_turns(words, since, until, LIMIT)
            turns.sort(key=lambda turn: turn.id)
        elif since or until:
            turns = await index.turns(since, until, LIMIT)
        else:
            return ActionResult(text=t("actions.history.need_query"), data={"ok": False})
        if not turns:
            return ActionResult(text=t("actions.history.none"), data={"count": 0})
        return ActionResult(
            text=t("actions.history.header") + "\n" + render_turns(turns, tz),
            data={"count": len(turns)},
        )
