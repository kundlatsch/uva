"""Builds the real :class:`Services` from settings (bot and worker share this)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from redis.asyncio import Redis

from uva.actions import build_action_registry
from uva.brain.vault import Vault
from uva.clock import utcnow
from uva.commands import build_registry
from uva.config import Settings
from uva.llm.budget import Budget
from uva.llm.client import build_llm_client
from uva.memory.service import MemoryService
from uva.logging import events, get_logger
from uva.search import build_search_backends
from uva.services import Services
from uva.storage import mongo
from uva.tasks.busy import HEARTBEAT_KEY, BusyTracker
from uva.tasks.celery_queue import CeleryTaskQueue

log = get_logger(__name__)


def build_services(settings: Settings) -> Services:
    client = mongo.connect(settings.mongo_uri)
    db = mongo.get_database(client)

    event_store = mongo.MongoEventStore(db, settings.events_ttl_days)
    events.set_store(event_store)
    usage = mongo.MongoUsageStore(db)
    llm = build_llm_client(settings, usage)

    services = Services(
        settings=settings,
        registry=build_registry(),
        actions=build_action_registry(),
        events=event_store,
        llm=llm,
        search=build_search_backends(settings, llm),
    )
    services.budget = Budget(usage, settings, services.tz)

    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    task_store = mongo.MongoTaskStore(db, settings.tasks_ttl_days)
    services.tasks = CeleryTaskQueue(task_store, redis)
    services.busy = BusyTracker(redis)

    services.vault = Vault(Path(settings.second_brain_path), services.tz)
    memory_store = mongo.MongoMemoryStore(db)
    services.memory = MemoryService(memory_store, settings)

    services.health_checks["MongoDB"] = lambda: mongo.ping(client)
    services.health_checks["Redis"] = lambda: _redis_ping(redis)
    services.health_checks["Worker + agendador"] = lambda: _heartbeat_fresh(redis)
    services.setup_hooks.extend(
        [
            event_store.setup,
            usage.setup,
            task_store.setup,
            memory_store.setup,
            # "At startup": compute the working memory once.
            services.memory.recompute_working_set,
            lambda: _setup_vault(services.vault),
        ]
    )
    return services


async def _setup_vault(vault: Vault) -> None:
    if await asyncio.to_thread(vault.ensure_structure):
        log.info("vault_created", path=str(vault.root))


async def _redis_ping(redis: Redis) -> bool:
    try:
        return bool(await redis.ping())
    except Exception:
        return False


async def _heartbeat_fresh(redis: Redis, max_age_seconds: int = 180) -> bool:
    """Beat schedules a heartbeat every minute and a worker runs it."""
    try:
        value = await redis.get(HEARTBEAT_KEY)
    except Exception:
        return False
    if not value:
        return False
    return (utcnow() - datetime.fromisoformat(value)).total_seconds() < max_age_seconds


async def setup_storage(services: Services) -> None:
    """Create indexes etc. A missing Mongo must not keep the bot from starting."""
    for hook in services.setup_hooks:
        try:
            await hook()
        except Exception as exc:
            log.warning("storage_setup_failed", step=getattr(hook, "__qualname__", "?"), error=str(exc))
