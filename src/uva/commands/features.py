"""/feature and /features: ask the bot to code a new command for itself."""

from __future__ import annotations

from uva.commands.registry import CommandContext, CommandRegistry
from uva.i18n import t
from uva.stm.refs import remember_ref
from uva.llm.budget import Scope
from uva.logging.events import EventType, current_trace_id, emit
from uva.selfmod.features import FeatureStatus, describe_feature

RECENT = 10


async def request_feature(ctx_services, description: str) -> str:
    """Shared by /feature and the feature_request action."""
    features = ctx_services.features
    if features is None or ctx_services.repo is None or ctx_services.llm is None:
        return t("commands.features.not_configured")
    if ctx_services.budget is not None:
        status = await ctx_services.budget.check(Scope.SELFCODE)
        if not status.allowed:
            return t("commands.features.over_budget", reason=status.reason)
    running = [
        r for r in await features.store.recent(5) if r.status in (FeatureStatus.QUEUED, FeatureStatus.RUNNING)
    ]
    if running:
        return f"{ctx_services.say('ocupado')}\n{t('commands.features.busy', id=running[0].id)}"
    record = await features.request(description, current_trace_id())
    await emit(EventType.SELFCODE, op="requested", feature_id=record.id, description=description)
    await remember_ref(ctx_services, "feature", record.id, f"feature: {description[:60]}", "na fila")
    return t("commands.features.requested", id=record.id)


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "feature",
        t("commands.features.feature.description"),
        aliases=("funcionalidade",),
        usage=t("commands.features.feature.usage_arg"),
    )
    async def feature(ctx: CommandContext) -> str:
        description = " ".join(ctx.args).strip()
        if len(description) < 8:
            return t("commands.features.feature.usage")
        return await request_feature(ctx.services, description)

    @registry.command(
        "features",
        t("commands.features.features.description"),
        aliases=("funcionalidades",),
        examples=("o que voce ja programou",),
    )
    async def features(ctx: CommandContext) -> str:
        if ctx.services.features is None:
            return t("commands.features.not_configured")
        records = await ctx.services.features.store.recent(RECENT)
        if not records:
            return t("commands.features.features.none")
        return t("commands.features.features.header") + "\n" + "\n".join(
            describe_feature(r) for r in records
        )
