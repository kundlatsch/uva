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
    from uva.commands.registry import CommandRegistry
    from uva.logging.events import EventStore

HealthCheck = Callable[[], Awaitable[bool]]


@dataclass
class Services:
    settings: Settings
    registry: CommandRegistry
    events: EventStore | None = None
    started_at: datetime = field(default_factory=utcnow)
    # Name -> async check returning True when the service is up (for /status).
    health_checks: dict[str, HealthCheck] = field(default_factory=dict)

    @property
    def tz(self) -> tzinfo:
        return get_tz(self.settings.timezone)
