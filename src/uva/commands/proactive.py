"""/proativo: see and control what the bot does on its own."""

from __future__ import annotations

from uva.commands.registry import CommandContext, CommandRegistry
from uva.logging.events import EventType, emit
from uva.replies import Reply


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "proativo",
        "Proatividade: status, ligar/desligar as iniciativas ou mandar uma agora",
        aliases=("proatividade", "iniciativas", "proactive"),
        usage="[on|off|agora]",
        examples=("o que voce faz sozinha", "status da proatividade"),
    )
    async def proactive(ctx: CommandContext) -> str | Reply:
        # Imported here: uva.initiatives pulls in the actions, which import the commands.
        from uva.initiatives import describe_status, initiative_tick, reply_for

        services = ctx.services
        arg = (ctx.args[0].lower() if ctx.args else "")
        if arg in ("on", "ligar", "liga"):
            if services.initiatives is None:
                return "Iniciativas indisponíveis (Mongo)."
            await services.initiatives.set_enabled(True)
            await emit(EventType.INITIATIVE, status="enabled")
            return "✅ Iniciativas ligadas. Vou aparecer de vez em quando (respeitando o silêncio e o limite do dia)."
        if arg in ("off", "desligar", "desliga"):
            if services.initiatives is None:
                return "Iniciativas indisponíveis (Mongo)."
            await services.initiatives.set_enabled(False)
            await emit(EventType.INITIATIVE, status="disabled")
            return "🔕 Iniciativas desligadas. Objetivos continuam rodando e avisando."
        if arg in ("agora", "now", "ja", "já"):
            status, message = await initiative_tick(services, force=True)
            return reply_for(message, status)
        if arg:
            return "Uso: /proativo [on|off|agora]"
        return Reply.paginated(await describe_status(services))
