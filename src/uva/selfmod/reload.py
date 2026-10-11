"""Hot reload of plugins in the running bot, coordinated through Redis.

The worker publishes a request on CHANNEL; the bot reloads its plugins and
writes the LoadReport to a per-request key, which the worker waits for.
"""

from __future__ import annotations

import asyncio
import json
import secrets

from redis.asyncio import Redis

from uva.logging import get_logger
from uva.logging.events import EventType, emit, trace
from uva.selfmod.plugins import LoadReport, load_plugins
from uva.services import Services

log = get_logger(__name__)

CHANNEL = "uva:plugins:reload"
RESULT_PREFIX = "uva:plugins:reload:"


async def reload_plugins(services: Services) -> LoadReport:
    report = load_plugins(services.registry, services.settings.plugins_dir)
    await emit(EventType.SELFCODE, op="reload", **report.to_dict())
    return report


async def request_reload(redis: Redis, timeout: float = 30) -> dict | None:
    """Ask the bot to reload; returns its report, or None if it didn't answer."""
    request_id = secrets.token_hex(4)
    await redis.publish(CHANNEL, request_id)
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        raw = await redis.get(RESULT_PREFIX + request_id)
        if raw:
            return json.loads(raw)
        await asyncio.sleep(0.5)
    return None


async def listen_for_reloads(services: Services, redis: Redis) -> None:
    """Runs forever in the bot; survives Redis restarts."""
    while True:
        try:
            pubsub = redis.pubsub()
            await pubsub.subscribe(CHANNEL)
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                with trace():
                    report = await reload_plugins(services)
                await redis.set(RESULT_PREFIX + message["data"], json.dumps(report.to_dict()), ex=300)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("reload_listener_error", error=str(exc))
            await asyncio.sleep(5)
