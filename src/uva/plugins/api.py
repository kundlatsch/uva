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
  * Plugins don't get access to the bot internals (memory, notes, secrets)
    and must not touch files, processes or the environment. Self-coded
    plugins are statically checked for that before they are accepted.
  * Each plugin has tests in ``tests/plugins/test_<name>.py``; use
    :func:`uva.plugins.testing.run` to call a handler from a test.
  * Command names and aliases must not clash with existing commands
    (see /comandos).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from types import ModuleType

Handler = Callable[["PluginContext"], "str | Awaitable[str]"]

MARKER = "uva_plugin_command"


@dataclass(frozen=True, slots=True)
class PluginContext:
    args: list[str] = field(default_factory=list)
    now: datetime | None = None

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
