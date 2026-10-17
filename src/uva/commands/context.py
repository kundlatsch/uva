"""/contexto: what the bot remembers about the current session."""

from __future__ import annotations

from uva.clock import format_local
from uva.commands.registry import CommandContext, CommandRegistry
from uva.i18n import t
from uva.stm.service import describe_ref

SHOWN_TURNS = 10


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "contexto",
        t("commands.context.contexto.description"),
        aliases=("context",),
        usage=t("commands.context.contexto.usage_arg"),
        examples=("do que estavamos falando",),
    )
    async def context(ctx: CommandContext) -> str:
        stm = ctx.services.stm
        if stm is None:
            return t("commands.context.contexto.unavailable")
        if ctx.args and ctx.args[0].lower() in ("limpar", "apagar", "zerar", "clear", "reset"):
            await stm.clear()
            return t("commands.context.contexto.cleared")
        session = await stm.session()
        if not session.turns and not session.refs:
            return t("commands.context.contexto.empty")
        tz = ctx.services.tz
        lines = [
            t("commands.context.contexto.header", turns=len(session.turns), refs=len(session.refs))
        ]
        if session.refs:
            lines.append(t("commands.context.contexto.refs"))
            lines += [describe_ref(r, tz, focus=r.ref == session.focus) for r in session.refs]
        if session.turns:
            lines.append(t("commands.context.contexto.turns"))
            for turn in session.turns[-SHOWN_TURNS:]:
                when = format_local(turn.at, tz, "%H:%M")
                role = t(f"commands.context.role.{turn.role}")
                lines.append(f"- {when} {role}: {turn.text[:120]}")
        return "\n".join(lines)
