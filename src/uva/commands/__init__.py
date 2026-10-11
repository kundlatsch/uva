"""Bot commands and the registry that holds them."""

from uva.commands.builtin import register_builtin, render_help
from uva.commands.registry import Command, CommandContext, CommandRegistry


def build_registry() -> CommandRegistry:
    """Registry with every command shipped by the bot."""
    registry = CommandRegistry()
    register_builtin(registry)
    return registry


__all__ = [
    "Command",
    "CommandContext",
    "CommandRegistry",
    "build_registry",
    "register_builtin",
    "render_help",
]
