"""Per-worker-process state: one event loop, one Services, one Telegram Bot.

Celery tasks are synchronous; our code is async. Each worker process keeps a
single asyncio.Runner so clients (Mongo, Redis, HTTP) bound to its loop can
be reused across tasks.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from telegram import Bot

from uva.bootstrap import build_services, setup_storage
from uva.config import get_settings
from uva.replies import Reply
from uva.services import Services
from uva.telegram_io import send_reply

T = TypeVar("T")

_runner: asyncio.Runner | None = None
_services: Services | None = None
_bot: Bot | None = None


def run(factory: Callable[[], Awaitable[T]]) -> T:
    global _runner
    if _runner is None:
        _runner = asyncio.Runner()
    return _runner.run(factory())


async def get_services() -> Services:
    global _services
    if _services is None:
        _services = build_services(get_settings())
        if _services.mcp is not None:
            _services.mcp.notifier = lambda text: notify(Reply(text))
        await setup_storage(_services)
    return _services


async def notify(reply: Reply) -> None:
    """Send a message to the owner. The worker never talks to anyone else."""
    global _bot
    settings = get_settings()
    if _bot is None:
        _bot = Bot(settings.telegram_bot_token.get_secret_value())
        await _bot.initialize()
    message_ids = await send_reply(_bot, settings.allowed_telegram_user_id, reply)
    if _services is not None and _services.sqlite is not None:
        from uva import sent

        try:  # what a quoted reply or a reaction to this message is about
            await sent.record(_services.sqlite, message_ids, reply.kind or "notice", reply.ref,
                              subject=reply.subject)
        except Exception:
            pass
    # The bot should know what it told the user on its own (short-term memory
    # and the conversation log).
    if _services is not None and _services.index is not None:
        try:
            await _services.index.add_turn("notice", reply.text)
        except Exception:
            pass
    if _services is not None and _services.stm is not None:
        try:
            await _services.stm.add_turn("notice", reply.text)
        except Exception:
            pass
