"""/sh and /py: commands and Python in the bot's own folder /uva (a sandbox)."""

from __future__ import annotations

from uva.commands.registry import CommandContext, CommandRegistry
from uva.replies import Reply
from uva.shell.service import USAGE

NOT_CONFIGURED = "O /uva não está configurado (container shell)."


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "sh",
        "Roda um comando Linux na minha pasta /uva (isolada; apagar pede confirmação)",
        aliases=("shell", "terminal"),
        usage="<comando> | desfazer | historico",
    )
    async def sh(ctx: CommandContext) -> Reply:
        shell = ctx.services.shell
        if shell is None:
            return Reply(NOT_CONFIGURED)
        text = " ".join(ctx.args).strip()
        if text in ("desfazer", "undo", "voltar"):
            return await shell.undo()
        if text in ("historico", "histórico", "snapshots"):
            return await shell.history()
        if not text:
            return Reply(USAGE)
        return await shell.request("sh", ctx.raw_args or text)

    @registry.command(
        "py",
        "Roda código Python na minha pasta /uva (com pip num venv próprio)",
        aliases=("python",),
        usage="<código>",
    )
    async def py(ctx: CommandContext) -> Reply:
        shell = ctx.services.shell
        if shell is None:
            return Reply(NOT_CONFIGURED)
        code = (ctx.raw_args or " ".join(ctx.args)).strip()
        if not code:
            return Reply(USAGE)
        return await shell.request("py", code)
