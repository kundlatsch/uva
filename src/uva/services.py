"""Container for the long-lived dependencies shared by commands and actions.

Built once in ``main`` (or in a Celery worker); tests build it with fakes.
Optional fields stay ``None`` when a backing service is not configured.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import TYPE_CHECKING

from uva.clock import get_tz, utcnow
from uva.config import Settings

if TYPE_CHECKING:
    from uva.actions.registry import ActionRegistry
    from uva.brain.vault import Vault
    from uva.commands.registry import CommandRegistry
    from uva.goals.store import GoalStore
    from uva.llm.budget import Budget
    from uva.llm.client import LLMClient
    from uva.memory.service import MemoryService
    from uva.selfmod.features import FeatureService
    from uva.selfmod.files import FileAccess
    from uva.selfmod.git import GitRepo
    from uva.logging.events import EventStore
    from uva.search.base import SearchBackend
    from uva.storage.expenses import ExpenseRepository
    from uva.tasks.busy import BusyTracker
    from uva.tasks.queue import TaskQueue

HealthCheck = Callable[[], Awaitable[bool]]


@dataclass
class Services:
    settings: Settings
    registry: CommandRegistry
    actions: ActionRegistry | None = None
    events: EventStore | None = None
    llm: LLMClient | None = None
    budget: Budget | None = None
    # Search backends by source name ("web", "wikipedia").
    search: dict[str, SearchBackend] = field(default_factory=dict)
    tasks: TaskQueue | None = None
    vault: Vault | None = None
    memory: MemoryService | None = None
    goals: GoalStore | None = None
    expenses: ExpenseRepository | None = None
    repo: GitRepo | None = None
    files: FileAccess | None = None
    features: FeatureService | None = None
    # Called after code in the repo changed (e.g. a reverted plugin).
    on_code_changed: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    busy: BusyTracker | None = None
    started_at: datetime = field(default_factory=utcnow)
    # Name -> async check returning True when the service is up (for /status).
    health_checks: dict[str, HealthCheck] = field(default_factory=dict)
    # Async setup steps (index creation...) run once at startup.
    setup_hooks: list[Callable[[], Awaitable[None]]] = field(default_factory=list)

    @property
    def tz(self) -> tzinfo:
        return get_tz(self.settings.timezone)
