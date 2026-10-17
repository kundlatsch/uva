"""/liberar: allow more LLM spend today, beyond the daily budget."""

from __future__ import annotations

from uva.commands.registry import CommandContext, CommandRegistry
from uva.i18n import t
from uva.logging.events import EventType, emit
from uva.storage.expenses import parse_amount

MAX_EXTRA_USD = 50.0  # guard against typos like "/liberar 500"


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "liberar",
        t("commands.budget.liberar.description"),
        aliases=("liberar_llm", "unlock", "unlock_llm"),
        usage=t("commands.budget.liberar.usage_arg"),
        examples=("libera mais orcamento", "aumenta o limite de hoje"),
    )
    async def grant(ctx: CommandContext) -> str:
        services = ctx.services
        settings = services.settings
        if services.budget is None:
            return t("commands.budget.liberar.not_configured")
        if ctx.args:
            try:
                amount = parse_amount(ctx.args[0].upper().removeprefix("US$").removeprefix("$")) / 100
            except ValueError:
                return t("commands.budget.liberar.usage")
        else:
            amount = settings.llm_daily_budget_usd
        if not 0 < amount <= MAX_EXTRA_USD:
            return t("commands.budget.liberar.out_of_range", max=f"{MAX_EXTRA_USD:.0f}")
        try:
            total_extra = await services.budget.grant_extra(amount)
            spend = await services.budget.spend()
        except Exception as exc:
            return t("commands.budget.liberar.failed", error=exc)
        await emit(EventType.BUDGET, op="grant", amount=amount, extra_today=total_extra)
        return t(
            "commands.budget.liberar.granted",
            amount=f"{amount:.2f}",
            limit=f"{spend.daily_limit(settings):.2f}",
            today=f"{spend.today:.4f}",
            month=f"{spend.month:.2f}",
            monthly=f"{settings.llm_monthly_budget_usd:.2f}",
        )
