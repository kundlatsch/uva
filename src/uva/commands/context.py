"""/contexto: what the bot remembers about the current session."""

from __future__ import annotations

from uva.clock import format_local
from uva.commands.registry import CommandContext, CommandRegistry
from uva.i18n import t
from uva.stm.service import describe_ref
from uva.text import normalize

SHOWN_TURNS = 10
HISTORY_LIMIT = 15
HISTORY_CHARS = 200


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

    @registry.command(
        "historico",
        t("commands.context.historico.description"),
        aliases=("histórico", "history"),
        usage=t("commands.context.historico.usage_arg"),
        examples=("o que conversamos sobre", "procura nas conversas"),
    )
    async def history(ctx: CommandContext) -> str:
        index = ctx.services.index
        if index is None:
            return t("commands.context.historico.unavailable")
        term = " ".join(ctx.args).strip()
        if not term:
            return t("commands.context.historico.usage")
        turns = await index.search_turns(normalize(term).split(), limit=HISTORY_LIMIT)
        if not turns:
            return t("commands.context.historico.none", term=term)
        tz = ctx.services.tz
        lines = [t("commands.context.historico.header", term=term, count=len(turns))]
        for turn in sorted(turns, key=lambda turn: turn.id):
            when = format_local(turn.at, tz, "%d/%m/%Y %H:%M")
            role = t(f"commands.context.role.{turn.role}")
            text = " ".join(turn.text.split())
            if len(text) > HISTORY_CHARS:
                text = text[: HISTORY_CHARS - 1] + "…"
            lines.append(f"- {when} {role}: {text}")
        return "\n".join(lines)
