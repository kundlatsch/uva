"""Builds the real :class:`Services` from settings (bot and worker share this)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from redis.asyncio import Redis

from uva.actions import build_action_registry
from uva import i18n
from uva.audio import build_transcriber
from uva.brain.vault import Vault
from uva.clock import utcnow
from uva.commands import build_registry
from uva.config import Settings
from uva.llm.budget import Budget
from uva.llm.client import build_llm_client
from uva.llm.decisions import build_decisions_client
from uva.index.service import SearchIndex
from uva.memory.service import MemoryService, RedisWorkingVersion
from uva.logging import events, get_logger
from uva.search import build_search_backends
from uva.services import Services
from uva.projects.service import ProjectService
from uva.shell.service import ShellService
from uva.storage.stickers import StickerRepository
from uva.selfmod.features import FeatureService
from uva.selfmod.files import FileAccess
from uva.selfmod.git import GitRepo
from uva.stm.redis_backend import RedisSessionBackend
from uva.stm.service import ShortTermMemory
from uva.storage import mongo
from uva.storage.expenses import ExpenseRepository
from uva.storage.sqlite import Database
from uva.tasks.busy import HEARTBEAT_KEY, BusyTracker
from uva.tasks.celery_queue import CeleryTaskQueue

log = get_logger(__name__)


def build_services(settings: Settings) -> Services:
    i18n.set_language(settings.bot_language)
    client = mongo.connect(settings.mongo_uri)
    db = mongo.get_database(client)

    event_store = mongo.MongoEventStore(db, settings.events_ttl_days)
    events.set_store(event_store)
    usage = mongo.MongoUsageStore(db)
    llm = build_llm_client(settings, usage)

    services = Services(
        settings=settings,
        registry=build_registry(settings.plugins_dir),
        actions=build_action_registry(),
        events=event_store,
        llm=llm,
        search=build_search_backends(settings, llm),
    )
    extras = mongo.MongoExtraStore(db)
    services.budget = Budget(usage, settings, services.tz, extras)
    services.decisions = build_decisions_client(settings, usage)
    services.transcriber = build_transcriber(settings, usage)

    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    task_store = mongo.MongoTaskStore(db, settings.tasks_ttl_days)
    services.tasks = CeleryTaskQueue(task_store, redis)
    services.busy = BusyTracker(redis)
    services.stm = ShortTermMemory(RedisSessionBackend(redis), settings)

    services.vault = Vault(Path(settings.second_brain_path), services.tz)
    sqlite_db = Database(Path(settings.sqlite_path))
    services.index = SearchIndex(sqlite_db, services.vault)
    services.vault.on_change.append(services.index.note_changed)
    services.vault.on_remove.append(services.index.note_removed)
    memory_store = mongo.MongoMemoryStore(db)
    services.memory = MemoryService(
        memory_store, settings, version=RedisWorkingVersion(redis), index=services.index
    )
    goal_store = mongo.MongoGoalStore(db)
    services.goals = goal_store
    initiative_store = mongo.MongoInitiativeStore(db)
    services.initiatives = initiative_store
    services.expenses = ExpenseRepository(sqlite_db)
    services.stickers = StickerRepository(sqlite_db)

    repo_dir = settings.repo_dir
    if (repo_dir / ".git").exists():
        services.repo = GitRepo(repo_dir, settings.git_author_name, settings.git_author_email)
    services.files = FileAccess(
        repo_dir, services.vault.root, services.repo,
        caps={"data/profile.md": settings.profile_max_chars, "data/soul.md": settings.soul_max_chars},
    )
    services.brain_repo = GitRepo(services.vault.root, settings.git_author_name, settings.git_author_email)
    feature_store = mongo.MongoFeatureStore(db)
    services.features = FeatureService(feature_store, _enqueue_selfcode)
    project_store = mongo.MongoProjectStore(db)
    services.projects = ProjectService(project_store, settings, _enqueue_project)
    services.shell = ShellService(settings)
    routine_store = mongo.MongoRoutineStore(db)
    services.routines = routine_store

    services.health_checks["MongoDB"] = lambda: mongo.ping(client)
    services.health_checks["Redis"] = lambda: _redis_ping(redis)
    services.health_checks["Worker + agendador"] = lambda: _heartbeat_fresh(redis)
    services.setup_hooks.extend(
        [
            event_store.setup,
            usage.setup,
            task_store.setup,
            memory_store.setup,
            goal_store.setup,
            initiative_store.setup,
            feature_store.setup,
            project_store.setup,
            routine_store.setup,
            # "At startup": compute the working memory once.
            services.memory.recompute_working_set,
            lambda: _setup_vault(services.vault),
            lambda: _setup_sqlite(sqlite_db),
            lambda: setup_index(services),
        ]
    )
    return services


async def _enqueue_selfcode(feature_id: str) -> None:
    from uva.tasks.app import app  # lazy: the bot only needs Celery to enqueue

    await asyncio.to_thread(
        app.send_task, "uva.selfcode", kwargs={"feature_id": feature_id}, task_id=f"selfcode-{feature_id}"
    )


async def _enqueue_project(project_id: str) -> None:
    from uva.tasks.app import app

    await asyncio.to_thread(
        app.send_task, "uva.project_build", kwargs={"project_id": project_id},
        task_id=f"project-{project_id}", queue="projects",
    )


BRAIN_GITIGNORE = ".obsidian/workspace*\n.obsidian/cache\n.trash/\n"


def _init_brain_git(vault: Vault) -> bool:
    """Give the vault its own git repository (once), with a first commit."""
    root = vault.root
    if (root / ".git").exists() or not root.is_dir():
        return False
    import subprocess

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    (root / ".gitignore").write_text(BRAIN_GITIGNORE, encoding="utf-8")
    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("-c", "user.name=uva", "-c", "user.email=uva@localhost", "commit", "-q", "--allow-empty",
        "-m", "Estado inicial do second brain")
    return True


async def _setup_vault(vault: Vault) -> None:
    if await asyncio.to_thread(vault.ensure_structure):
        log.info("vault_created", path=str(vault.root))
    try:
        if await asyncio.to_thread(_init_brain_git, vault):
            log.info("brain_git_created", path=str(vault.root))
    except Exception as exc:  # no git in the vault is not fatal
        log.warning("brain_git_failed", error=str(exc))


async def _setup_sqlite(db: Database) -> None:
    applied = await asyncio.to_thread(db.migrate)
    if applied:
        log.info("sqlite_migrated", path=str(db.path), applied=applied)


async def setup_index(services: Services) -> None:
    """Build the search index the first time; afterwards only pick up notes
    changed while the bot was off."""
    index = services.index
    if index is None:
        return
    if await index.count() == 0:
        result = await rebuild_index(services)
        log.info("index_built", **result)
    else:
        result = await index.sync_notes()
        if result["indexed"] or result["removed"]:
            log.info("index_synced", **result)


async def rebuild_index(services: Services) -> dict[str, int]:
    """Recreate the index from the vault, Mongo memory, goals and the conversation log."""
    assert services.index is not None
    memory = await services.memory.store.all() if services.memory is not None else []
    goals = await services.goals.list() if services.goals is not None else []
    return await services.index.rebuild(memory, goals)


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
