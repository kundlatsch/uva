"""Bot commands and the registry that holds them."""

from pathlib import Path

from uva.commands import (
    brain,
    budget,
    builtin,
    catalog,
    context,
    decide,
    expenses,
    features,
    goals,
    llm,
    log,
    memory,
    phrases,
    proactive,
    projects,
    revert,
    routines,
    shell,
    soul,
    status,
    stickers,
    tasks,
)
from uva.commands.builtin import render_help
from uva.commands.registry import Command, CommandContext, CommandRegistry

# Order here is the order shown in /help.
_MODULES = (
    builtin,
    brain,
    memory,
    stickers,
    context,
    goals,
    routines,
    proactive,
    expenses,
    decide,
    log,
    status,
    llm,
    budget,
    soul,
    phrases,
    revert,
    features,
    projects,
    shell,
    tasks,
    catalog,
)


def build_registry(plugins_dir: Path | None = None) -> CommandRegistry:
    """Registry with the core commands plus every plugin in ``plugins_dir``
    (default: the installed uva/plugins folder)."""
    from uva.selfmod.plugins import default_plugins_dir, load_plugins

    registry = CommandRegistry()
    for module in _MODULES:
        module.register(registry)
    load_plugins(registry, plugins_dir or default_plugins_dir())
    return registry


__all__ = [
    "Command",
    "CommandContext",
    "CommandRegistry",
    "build_registry",
    "render_help",
]
