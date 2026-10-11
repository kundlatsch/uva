"""Built-in commands available since phase 1."""

from __future__ import annotations

from uva.commands.registry import CommandContext, CommandRegistry


def register(registry: CommandRegistry) -> None:
    @registry.command("start", "Apresentação do bot", aliases=("inicio", "início"))
    async def start(ctx: CommandContext) -> str:
        return (
            "Olá! Eu sou a UVA, sua assistente pessoal.\n"
            "Envie /help para ver os comandos, ou simplesmente me escreva."
        )

    @registry.command("help", "Lista os comandos disponíveis", aliases=("ajuda", "comandos"))
    async def help_(ctx: CommandContext) -> str:
        return render_help(ctx.registry)

    @registry.command("ping", "Verifica se o bot está vivo")
    async def ping(ctx: CommandContext) -> str:
        return "pong"


def render_help(registry: CommandRegistry) -> str:
    """Help text generated from the registry, so new commands show up automatically."""
    lines = ["Comandos disponíveis:"]
    for cmd in registry:
        line = f"/{cmd.name}"
        if cmd.usage:
            line += f" {cmd.usage}"
        line += f" - {cmd.description}"
        if cmd.aliases:
            line += f" (também: {', '.join(cmd.aliases)})"
        lines.append(line)
    return "\n".join(lines)
