"""Builds the real :class:`Services` from settings (bot and worker share this)."""

from __future__ import annotations

from uva.actions import build_action_registry
from uva.commands import build_registry
from uva.config import Settings
from uva.llm.budget import Budget
from uva.llm.client import build_llm_client
from uva.logging import events, get_logger
from uva.search import build_search_backends
from uva.services import Services
from uva.storage import mongo

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
    services.health_checks["MongoDB"] = lambda: mongo.ping(client)
    services.setup_hooks.extend([event_store.setup, usage.setup])
    return services


async def setup_storage(services: Services) -> None:
    """Create indexes etc. A missing Mongo must not keep the bot from starting."""
    for hook in services.setup_hooks:
        try:
            await hook()
        except Exception as exc:
            log.warning("storage_setup_failed", step=getattr(hook, "__qualname__", "?"), error=str(exc))
