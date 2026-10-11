"""Bot commands and the registry that holds them."""

from uva.commands import builtin, llm, log, status, tasks
from uva.commands.builtin import render_help
from uva.commands.registry import Command, CommandContext, CommandRegistry

# Order here is the order shown in /help.
_MODULES = (builtin, log, status, llm, tasks)


def build_registry() -> CommandRegistry:
    """Registry with every command shipped by the bot."""
    registry = CommandRegistry()
    for module in _MODULES:
        module.register(registry)
    return registry


__all__ = [
    "Command",
    "CommandContext",
    "CommandRegistry",
    "build_registry",
    "render_help",
]
