"""Built-in commands available since phase 1."""

from __future__ import annotations

from uva.commands.registry import CommandContext, CommandRegistry
from uva.i18n import t


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "start",
        t("commands.builtin.start.description"),
        aliases=("inicio", "início", "iniciar"),
        examples=("quem e voce", "se apresenta"),
    )
    async def start(ctx: CommandContext) -> str:
        return t("commands.builtin.start.reply")

    @registry.command(
        "help",
        t("commands.builtin.help.description"),
        aliases=("ajuda",),
        examples=("o que voce sabe fazer", "o que voce faz", "socorro"),
    )
    async def help_(ctx: CommandContext) -> str:
        return render_help(ctx.registry)

    @registry.command(
        "ping",
        t("commands.builtin.ping.description"),
        examples=("esta viva", "ta ai", "voce esta ai"),
    )
    async def ping(ctx: CommandContext) -> str:
        return "pong"


def render_help(registry: CommandRegistry) -> str:
    """Help text generated from the registry, so new commands show up automatically."""
    lines = [t("commands.builtin.help.header")]
    for cmd in registry:
        line = f"/{cmd.name}"
        if cmd.usage:
            line += f" {cmd.usage}"
        line += f" - {cmd.description}"
        if cmd.aliases:
            line += t("commands.builtin.help.aliases", aliases=", ".join(cmd.aliases))
        lines.append(line)
    return "\n".join(lines)
