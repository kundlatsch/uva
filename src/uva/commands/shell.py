"""/sh and /py: commands and Python in the bot's own folder /uva (a sandbox)."""

from __future__ import annotations

from uva.commands.registry import CommandContext, CommandRegistry
from uva.i18n import t
from uva.replies import Reply
from uva.shell.service import usage


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "sh",
        t("commands.shell.sh.description"),
        aliases=("shell", "terminal"),
        usage=t("commands.shell.sh.usage_arg"),
    )
    async def sh(ctx: CommandContext) -> Reply:
        shell = ctx.services.shell
        if shell is None:
            return Reply(t("commands.shell.not_configured"))
        text = " ".join(ctx.args).strip()
        if text in ("desfazer", "undo", "voltar"):
            return await shell.undo()
        if text in ("historico", "histórico", "history", "snapshots"):
            return await shell.history()
        if not text:
            return Reply(usage())
        return await shell.request("sh", ctx.raw_args or text)

    @registry.command(
        "py",
        t("commands.shell.py.description"),
        aliases=("python",),
        usage=t("commands.shell.py.usage_arg"),
    )
    async def py(ctx: CommandContext) -> Reply:
        shell = ctx.services.shell
        if shell is None:
            return Reply(t("commands.shell.not_configured"))
        code = (ctx.raw_args or " ".join(ctx.args)).strip()
        if not code:
            return Reply(usage())
        return await shell.request("py", code)
