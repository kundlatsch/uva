"""Plugin interface: how to add a command without touching the core.

A plugin is one module ``src/uva/plugins/<name>.py`` (written by Gustavo or
by the bot itself via /feature). The command registry discovers every module
in this folder at startup and on hot reload. Example::

    import random

    from uva.plugins.api import PluginContext, command

    TEXTS = {
        "en": {"faces": ["Heads!", "Tails!"]},
        "pt": {"faces": ["Cara!", "Coroa!"]},
    }


    @command(
        "moeda",
        {"en": "Flips a coin: heads or tails", "pt": "Joga uma moeda: cara ou coroa"},
        aliases=("coin",),
        examples=("joga uma moeda", "cara ou coroa", "flip a coin"),
    )
    def coin(ctx: PluginContext) -> str:
        texts = TEXTS.get(ctx.lang, TEXTS["en"])
        return random.choice(texts["faces"])

Rules:
  * A handler receives a :class:`PluginContext` and returns the reply text
    (``str``). It may be a plain function or ``async def``.
  * ``ctx.args`` are the words after the command (``/dado 20`` -> ``["20"]``);
    ``ctx.text`` is them joined; ``ctx.now`` is the current local time.
  * ``ctx.lang`` is the bot language, ``"en"`` or ``"pt"``. Every text the user
    reads (replies, usage messages) must exist in both languages: keep them in
    the plugin itself, e.g. a ``TEXTS = {"en": {...}, "pt": {...}}`` dict, and
    pick with ``TEXTS.get(ctx.lang, TEXTS["en"])``. Plugins can't import the
    bot's translation module.
  * The ``@command`` description (and ``usage``, when it has words) may be a
    plain string or a dict ``{"en": "...", "pt": "..."}`` (preferred); /help
    shows the one in the bot language. ``examples`` are phrases matched against what the user types:
    they may mix both languages.
  * ``ctx.data`` is a READ-ONLY view of the bot's data (see :class:`DataView`):
    memories, goals, the recent short-term context, expenses and note titles.
    Its methods are ``async`` and return plain dicts/lists, so a handler that
    uses it must be ``async def``. It is the only way a plugin can see data;
    there are no write methods and no access to databases, files or secrets.
  * Plugins must not touch files, processes or the environment. Self-coded
    plugins are statically checked for that before they are accepted.
  * Each plugin has tests in ``tests/plugins/test_<name>.py``; use
    :func:`uva.plugins.testing.run` to call a handler from a test (pass
    ``lang="en"`` or ``lang="pt"``; the default is ``"pt"``), and
    :class:`uva.plugins.testing.StaticData` to give it fake ``ctx.data``.
  * Command names and aliases must not clash with existing commands
    (see /comandos).

Example using data::

    TEXTS = {
        "en": {"next": "next", "empty": "Nothing here."},
        "pt": {"next": "próxima", "empty": "Nada por aqui."},
    }


    @command(
        "resumo",
        {"en": "Working memory and active goals", "pt": "Memória de trabalho e objetivos ativos"},
    )
    async def summary(ctx: PluginContext) -> str:
        texts = TEXTS.get(ctx.lang, TEXTS["en"])
        memories = await ctx.data.working_memory(limit=3)
        goals = await ctx.data.goals(status="ativo")
        lines = [f"- {m['key']}: {m['value']}" for m in memories]
        lines += [f"🎯 {g['title']} ({texts['next']}: {g['next_run']})" for g in goals]
        return "\n".join(lines) or texts["empty"]
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from types import ModuleType
from typing import Any, Protocol

Handler = Callable[["PluginContext"], "str | Awaitable[str]"]
# A command description: one text, or one per language ({"en": ..., "pt": ...}).
Description = str | dict[str, str]

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
    lang: str = "pt"  # bot language: "en" | "pt"

    @property
    def text(self) -> str:
        return " ".join(self.args)


@dataclass(frozen=True, slots=True)
class PluginCommand:
    name: str
    description: Description
    handler: Handler
    aliases: tuple[str, ...] = ()
    usage: Description = ""
    examples: tuple[str, ...] = ()
    placeholder: str | None = None


def command(
    name: str,
    description: Description,
    *,
    aliases: tuple[str, ...] = (),
    usage: Description = "",
    examples: tuple[str, ...] = (),
    placeholder: str | None = None,
) -> Callable[[Handler], Handler]:
    """Mark a function as a bot command.

    ``placeholder``: for commands that take long, a ready-made phrase category
    ("executando", "pesquisando"...) shown at once and replaced by the reply.
    """

    def decorator(func: Handler) -> Handler:
        setattr(func, MARKER, PluginCommand(name, description, func, aliases, usage, examples, placeholder))
        return func

    return decorator


def resolve_description(description: Description, lang: str) -> str:
    """The description in ``lang`` (falls back to English, then to any text)."""
    if isinstance(description, str):
        return description
    for key in (lang, "en"):
        if description.get(key):
            return description[key]
    return next((text for text in description.values() if text), "")


def commands_in(module: ModuleType) -> list[PluginCommand]:
    """Every function in ``module`` decorated with :func:`command`."""
    found = [getattr(obj, MARKER) for obj in vars(module).values() if hasattr(obj, MARKER)]
    return sorted(found, key=lambda c: c.name)
