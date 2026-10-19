"""/sh and /py: commands and Python in the bot's own folder /uva (a sandbox)."""

from __future__ import annotations

from uva.commands.registry import CommandContext, CommandRegistry
from uva.i18n import t
from uva.replies import Reply
from uva.shell.service import usage


async def allowlist(shell, args: list[str]) -> Reply:
    """/sh permitidos [remover <n>]: the commands that run without asking."""
    if len(args) >= 2 and args[0].lower() in ("remover", "remove", "apagar", "delete") and args[1].isdigit():
        if await shell.disallow(int(args[1])):
            return Reply(t("shell.service.allowlist.removed", id=args[1]))
        return Reply(t("shell.service.allowlist.not_found", id=args[1]))
    entries = await shell.allowlist()
    if not entries:
        return Reply(t("shell.service.allowlist.empty"))
    lines = [t("shell.service.allowlist.header")]
    for entry_id, kind, code in entries:
        shown = code if len(code) <= 200 else code[:200] + "…"
        lines.append(t("shell.service.allowlist.line", id=entry_id, prompt="🐍" if kind == "py" else "$", code=shown))
    lines.append(t("shell.service.allowlist.hint"))
    return Reply("\n".join(lines))


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
        if ctx.args and ctx.args[0].lower() in ("permitidos", "allowed", "allowlist"):
            return await allowlist(shell, ctx.args[1:])
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
