"""/contexto: what the bot remembers about the current session."""

from __future__ import annotations

from uva.clock import format_local
from uva.commands.registry import CommandContext, CommandRegistry
from uva.stm.service import describe_ref

ROLE = {"user": "você", "bot": "eu", "notice": "eu (aviso)"}
SHOWN_TURNS = 10


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "contexto",
        "O que eu lembro da conversa de agora (ou 'limpar')",
        usage="[limpar]",
        examples=("do que estavamos falando",),
    )
    async def context(ctx: CommandContext) -> str:
        stm = ctx.services.stm
        if stm is None:
            return "Memória de curto prazo indisponível (Redis)."
        if ctx.args and ctx.args[0].lower() in ("limpar", "apagar", "zerar"):
            await stm.clear()
            return "Contexto apagado. Começando do zero."
        session = await stm.session()
        if not session.turns and not session.refs:
            return "Nada no contexto agora."
        tz = ctx.services.tz
        lines = [f"Contexto recente ({len(session.turns)} turnos, {len(session.refs)} referências)"]
        if session.refs:
            lines.append("\nReferências:")
            lines += [describe_ref(r, tz, focus=r.ref == session.focus) for r in session.refs]
        if session.turns:
            lines.append("\nÚltimos turnos:")
            for turn in session.turns[-SHOWN_TURNS:]:
                when = format_local(turn.at, tz, "%H:%M")
                lines.append(f"- {when} {ROLE[turn.role]}: {turn.text[:120]}")
        return "\n".join(lines)
