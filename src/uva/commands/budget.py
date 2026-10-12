"""/liberar: allow more LLM spend today, beyond the daily budget."""

from __future__ import annotations

from uva.commands.registry import CommandContext, CommandRegistry
from uva.logging.events import EventType, emit
from uva.storage.expenses import parse_amount

MAX_EXTRA_USD = 50.0  # guard against typos like "/liberar 500"


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "liberar",
        "Libera gasto extra de LLM hoje (sem valor: mais um limite diário)",
        aliases=("liberar_llm",),
        usage="[valor em US$]",
        examples=("libera mais orcamento", "aumenta o limite de hoje"),
    )
    async def grant(ctx: CommandContext) -> str:
        services = ctx.services
        settings = services.settings
        if services.budget is None:
            return "O orçamento não está configurado."
        if ctx.args:
            try:
                amount = parse_amount(ctx.args[0].upper().removeprefix("US$").removeprefix("$")) / 100
            except ValueError:
                return "Uso: /liberar [valor em US$], ex.: /liberar 2 ou /liberar 0,50"
        else:
            amount = settings.llm_daily_budget_usd
        if not 0 < amount <= MAX_EXTRA_USD:
            return f"O valor precisa ficar entre US$ 0,01 e US$ {MAX_EXTRA_USD:.0f}."
        try:
            total_extra = await services.budget.grant_extra(amount)
            spend = await services.budget.spend()
        except Exception as exc:
            return f"Não consegui liberar agora: {exc}"
        await emit(EventType.BUDGET, op="grant", amount=amount, extra_today=total_extra)
        return (
            f"✅ Liberei US$ {amount:.2f} a mais para hoje.\n"
            f"Limite de hoje: US$ {spend.daily_limit(settings):.2f} "
            f"(gasto até agora: US$ {spend.today:.4f}). Vale até a meia-noite.\n"
            f"O limite mensal continua valendo: US$ {spend.month:.2f} de "
            f"{settings.llm_monthly_budget_usd:.2f}."
        )
