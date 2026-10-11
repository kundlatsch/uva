"""/feature and /features: ask the bot to code a new command for itself."""

from __future__ import annotations

from uva.commands.registry import CommandContext, CommandRegistry
from uva.llm.budget import Scope
from uva.logging.events import EventType, current_trace_id, emit
from uva.selfmod.features import FeatureStatus, describe_feature

NOT_CONFIGURED = "Auto-código não está configurado (precisa de repositório, LLM e worker)."
RECENT = 10


async def request_feature(ctx_services, description: str) -> str:
    """Shared by /feature and the feature_request action."""
    features = ctx_services.features
    if features is None or ctx_services.repo is None or ctx_services.llm is None:
        return NOT_CONFIGURED
    if ctx_services.budget is not None:
        status = await ctx_services.budget.check(Scope.SELFCODE)
        if not status.allowed:
            return f"Agora não dá: {status.reason}."
    running = [
        r for r in await features.store.recent(5) if r.status in (FeatureStatus.QUEUED, FeatureStatus.RUNNING)
    ]
    if running:
        return f"{ctx_services.say('ocupado')}\n(feature {running[0].id} ainda em construção; veja /features)"
    record = await features.request(description, current_trace_id())
    await emit(EventType.SELFCODE, op="requested", feature_id=record.id, description=description)
    return (
        f"🛠️ Beleza, vou construir isso (feature {record.id}). Escrevo o plugin, rodo todos os "
        "testes e te aviso quando estiver no ar. Acompanhe em /features."
    )


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "feature",
        "Pede para eu programar um comando novo para mim mesma",
        aliases=("funcionalidade",),
        usage="<descrição>",
    )
    async def feature(ctx: CommandContext) -> str:
        description = " ".join(ctx.args).strip()
        if len(description) < 8:
            return "Uso: /feature <descrição>, ex.: /feature /dado rola um dado de 6 lados"
        return await request_feature(ctx.services, description)

    @registry.command(
        "features",
        "Funcionalidades que eu programei (ou tentei)",
        examples=("o que voce ja programou",),
    )
    async def features(ctx: CommandContext) -> str:
        if ctx.services.features is None:
            return NOT_CONFIGURED
        records = await ctx.services.features.store.recent(RECENT)
        if not records:
            return "Ainda não programei nada. Peça com /feature <descrição>."
        return "Funcionalidades:\n" + "\n".join(describe_feature(r) for r in records)
