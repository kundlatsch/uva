"""/llm: which provider and models are in use, and how much was spent."""

from __future__ import annotations

from uva.commands.registry import CommandContext, CommandRegistry
from uva.services import Services


async def describe_spend(services: Services) -> list[str]:
    s = services.settings
    if services.budget is None:
        return ["Gasto: indisponível"]
    try:
        spend = await services.budget.spend()
    except Exception:
        return ["Gasto: indisponível (Mongo fora?)"]
    extra = f" (inclui US$ {spend.extra_today:.2f} liberados hoje)" if spend.extra_today else ""
    return [
        f"Gasto hoje: US$ {spend.today:.4f} de {spend.daily_limit(s):.2f}{extra}",
        f"Gasto no mês: US$ {spend.month:.4f} de {s.llm_monthly_budget_usd:.2f}",
        f"Proativo hoje: US$ {spend.proactive_today:.4f} de {s.proactive_daily_budget_usd:.2f}",
        f"Auto-código hoje: US$ {spend.selfcode_today:.4f} de {s.selfcode_daily_budget_usd:.2f}",
    ]


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "llm",
        "Provedor, modelos e gasto da LLM",
        aliases=("ia", "modelo", "model"),
        examples=("qual modelo voce usa", "qual ia voce usa"),
    )
    async def llm(ctx: CommandContext) -> str:
        services = ctx.services
        s = services.settings
        lines = [f"Provedor: {s.llm_provider.value}"]
        if s.llm_base_url:
            lines.append(f"URL: {s.llm_base_url}")
        if services.llm is None:
            lines.append("LLM desligada (falta LLM_API_KEY ou LLM_MODEL_ROUTER no .env)")
        else:
            lines.append(f"Modelo rápido (roteamento): {services.llm.router_model}")
            lines.append(f"Modelo pesado (resumos, objetivos): {services.llm.heavy_model}")
            unknown = [
                m
                for m in {services.llm.router_model, services.llm.heavy_model}
                if not services.llm.prices.knows(m)
            ]
            if unknown:
                lines.append(
                    f"⚠️ Sem preço configurado para {', '.join(sorted(unknown))}; usando "
                    f"US$ {s.llm_default_price_input}/{s.llm_default_price_output} por 1M "
                    "tokens (ajuste LLM_PRICES)"
                )
        search = ", ".join(f"{k}={v.name}" for k, v in services.search.items()) or "nenhuma"
        lines.append(f"Pesquisa: {search}")
        lines.extend(await describe_spend(services))
        return "\n".join(lines)
