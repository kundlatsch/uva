"""Plugin interface: how to add a command without touching the core.

A plugin is one module ``src/uva/plugins/<name>.py`` (written by Gustavo or
by the bot itself via /feature). The command registry discovers every module
in this folder at startup and on hot reload. Example::

    import random

    from uva.plugins.api import PluginContext, command


    @command(
        "moeda",
        "Joga uma moeda: cara ou coroa",
        aliases=("coin",),
        examples=("joga uma moeda", "cara ou coroa"),
    )
    def coin(ctx: PluginContext) -> str:
        return random.choice(["Cara!", "Coroa!"])

Rules:
  * A handler receives a :class:`PluginContext` and returns the reply text
    (``str``). It may be a plain function or ``async def``.
  * ``ctx.args`` are the words after the command (``/dado 20`` -> ``["20"]``);
    ``ctx.text`` is them joined; ``ctx.now`` is the current local time.
  * ``ctx.data`` is a READ-ONLY view of the bot's data (see :class:`DataView`):
    memories, goals, the recent short-term context, expenses and note titles.
    Its methods are ``async`` and return plain dicts/lists, so a handler that
    uses it must be ``async def``. It is the only way a plugin can see data;
    there are no write methods and no access to databases, files or secrets.
  * Plugins must not touch files, processes or the environment. Self-coded
    plugins are statically checked for that before they are accepted.
  * Each plugin has tests in ``tests/plugins/test_<name>.py``; use
    :func:`uva.plugins.testing.run` to call a handler from a test, and
    :class:`uva.plugins.testing.StaticData` to give it fake ``ctx.data``.

Example using data::

    @command("resumo", "Memória de trabalho e objetivos ativos")
    async def summary(ctx: PluginContext) -> str:
        memories = await ctx.data.working_memory(limit=3)
        goals = await ctx.data.goals(status="ativo")
        lines = [f"- {m['key']}: {m['value']}" for m in memories]
        lines += [f"🎯 {g['title']} (próxima: {g['next_run']})" for g in goals]
        return "\n".join(lines) or "Nada por aqui."
  * Command names and aliases must not clash with existing commands
    (see /comandos).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from types import ModuleType
from typing import Any, Protocol

Handler = Callable[["PluginContext"], "str | Awaitable[str]"]

MARKER = "uva_plugin_command"


class DataView(Protocol):
    """Read-only data available to plugins. Every method returns plain values.

    Dates/times are already formatted in local time ("11/10 14:38").
    """

    async def working_memory(self, limit: int = 10) -> list[dict[str, Any]]:
        """Most relevant memories: [{"key", "value", "category"}]."""
        ...

    async def memories(self, limit: int = 50) -> list[dict[str, Any]]:
        """All memories, best score first: [{"key", "value", "category", "score"}]."""
        ...

    async def memory(self, key: str) -> dict[str, Any] | None:
        """One memory by key, or None."""
        ...

    async def goals(self, status: str | None = None) -> list[dict[str, Any]]:
        """Goals ("ativo" | "pausado" | "concluido" filter): [{"id", "title",
        "status", "priority", "kind", "next_run", "last_attempt", "last_attempt_at",
        "run_count"}]."""
        ...

    async def goal(self, goal_id: str) -> dict[str, Any] | None: ...

    async def recent_refs(self, limit: int = 10) -> list[dict[str, Any]]:
        """Things the bot did recently: [{"ref", "tipo", "rotulo", "status", "quando"}]."""
        ...

    async def expenses_summary(self, period: str = "mes") -> dict[str, Any]:
        """period: hoje | semana | mes | ano | tudo. Returns {"period", "total",
        "total_cents", "by_category": [{"category", "total", "total_cents"}]}
        (totals formatted like "R$ 25,90")."""
        ...

    async def recent_notes(self, limit: int = 10) -> list[dict[str, Any]]:
        """Most recently changed notes: [{"title", "path", "folder", "changed"}]."""
        ...


class NoData:
    """DataView with nothing in it (used when no bot is around, e.g. in tests)."""

    async def working_memory(self, limit: int = 10) -> list[dict[str, Any]]:
        return []

    async def memories(self, limit: int = 50) -> list[dict[str, Any]]:
        return []

    async def memory(self, key: str) -> dict[str, Any] | None:
        return None

    async def goals(self, status: str | None = None) -> list[dict[str, Any]]:
        return []

    async def goal(self, goal_id: str) -> dict[str, Any] | None:
        return None

    async def recent_refs(self, limit: int = 10) -> list[dict[str, Any]]:
        return []

    async def expenses_summary(self, period: str = "mes") -> dict[str, Any]:
        return {"period": period, "total": "R$ 0,00", "total_cents": 0, "by_category": []}

    async def recent_notes(self, limit: int = 10) -> list[dict[str, Any]]:
        return []


@dataclass(frozen=True, slots=True)
class PluginContext:
    args: list[str] = field(default_factory=list)
    now: datetime | None = None
    data: DataView = field(default_factory=NoData)

    @property
    def text(self) -> str:
        return " ".join(self.args)


@dataclass(frozen=True, slots=True)
class PluginCommand:
    name: str
    description: str
    handler: Handler
    aliases: tuple[str, ...] = ()
    usage: str = ""
    examples: tuple[str, ...] = ()


def command(
    name: str,
    description: str,
    *,
    aliases: tuple[str, ...] = (),
    usage: str = "",
    examples: tuple[str, ...] = (),
) -> Callable[[Handler], Handler]:
    """Mark a function as a bot command."""

    def decorator(func: Handler) -> Handler:
        setattr(func, MARKER, PluginCommand(name, description, func, aliases, usage, examples))
        return func

    return decorator


def commands_in(module: ModuleType) -> list[PluginCommand]:
    """Every function in ``module`` decorated with :func:`command`."""
    found = [getattr(obj, MARKER) for obj in vars(module).values() if hasattr(obj, MARKER)]
    return sorted(found, key=lambda c: c.name)
