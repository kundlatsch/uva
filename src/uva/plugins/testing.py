"""Helpers for plugin tests (keep plugin tests plain synchronous functions)."""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime

from uva.plugins.api import Handler, PluginContext


def run(handler: Handler, *args: str, now: datetime | None = None) -> str:
    """Call a plugin handler as if the user typed ``/<command> <args...>``."""
    result = handler(PluginContext(args=list(args), now=now or datetime.now()))
    if inspect.isawaitable(result):
        result = asyncio.run(_await(result))
    return str(result)


async def _await(awaitable):
    return await awaitable
