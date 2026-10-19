"""/soul: show the personality file. /perfil: size of the always-loaded files
and profile consolidation."""

from __future__ import annotations

from uva.commands.registry import CommandContext, CommandRegistry
from uva.i18n import t
from uva.replies import Reply
from uva.soul import Soul

CONSOLIDATE_WORDS = ("consolidar", "consolidate", "resumir", "compactar")


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "soul",
        t("commands.soul.soul.description"),
        aliases=("alma", "personalidade", "personality"),
        examples=("qual a sua personalidade",),
    )
    async def soul(ctx: CommandContext) -> Reply | str:
        path = ctx.services.settings.soul_file
        text = Soul(path).text()
        if not text:
            return t("commands.soul.soul.missing", path=path)
        return Reply(text=text, filename="soul.md").fit_for_telegram()

    @registry.command(
        "perfil",
        t("commands.soul.perfil.description"),
        aliases=("profile",),
        usage=t("commands.soul.perfil.usage_arg"),
        examples=("tamanho do perfil",),
    )
    async def profile(ctx: CommandContext) -> str:
        from uva import profile as profile_budget

        if ctx.args and ctx.args[0].lower() in CONSOLIDATE_WORDS:
            return await profile_budget.consolidate(ctx.services)
        lines = [t("commands.soul.perfil.header")]
        lines += [u.line() for u in profile_budget.usage(ctx.services)]
        if any(u.over for u in profile_budget.usage(ctx.services) if u.name == "profile.md"):
            lines.append(t("commands.soul.perfil.hint"))
        return "\n".join(lines)
