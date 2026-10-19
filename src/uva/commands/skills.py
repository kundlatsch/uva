"""/skills (list) and /skill <name> [request] (show it, or do a request with it)."""

from __future__ import annotations

from uva.clock import format_local
from uva.commands.registry import CommandContext, CommandRegistry
from uva.i18n import t
from uva.replies import Reply
from uva.skills import render


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "skills",
        t("commands.skills.skills.description"),
        aliases=("habilidades",),
        examples=("quais habilidades voce tem",),
    )
    async def skills(ctx: CommandContext) -> str:
        library = ctx.services.skills
        if library is None:
            return t("commands.skills.unavailable")
        found = library.all()
        if not found:
            return t("commands.skills.skills.empty")
        usage = await library.usage()
        tz = ctx.services.tz
        lines = [t("commands.skills.skills.header", count=len(found))]
        for skill in found:
            used = usage.get(skill.name)
            last = format_local(used.last_used_at, tz, "%d/%m %H:%M") if used and used.last_used_at else "—"
            idle = t("commands.skills.skills.idle") if skill.name in library.idle else ""
            lines.append(t(
                "commands.skills.skills.line", name=skill.name, description=skill.description,
                uses=used.uses if used else 0, last=last, idle=idle,
            ))
        lines.append(t("commands.skills.skills.hint"))
        return "\n".join(lines)

    @registry.command(
        "skill",
        t("commands.skills.skill.description"),
        aliases=("habilidade",),
        usage=t("commands.skills.skill.usage_arg"),
    )
    async def skill(ctx: CommandContext) -> Reply | str:
        library = ctx.services.skills
        if library is None:
            return t("commands.skills.unavailable")
        if not ctx.args:
            return t("commands.skills.skill.usage")
        found = library.get(ctx.args[0])
        if found is None:
            return t("commands.skills.skill.unknown", name=ctx.args[0])
        request = ctx.raw_args.strip()[len(ctx.args[0]):].strip()
        if not request:
            return Reply(text=render(found), filename=f"{found.name}.md").fit_for_telegram()
        if ctx.ask is None:
            return t("commands.skills.skill.no_llm")
        return await ctx.ask(request, skill=found)
