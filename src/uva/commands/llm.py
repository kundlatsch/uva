"""/llm: which provider and models are in use, and how much was spent."""

from __future__ import annotations

from uva.commands.registry import CommandContext, CommandRegistry
from uva.i18n import t
from uva.services import Services


async def describe_spend(services: Services) -> list[str]:
    s = services.settings
    if services.budget is None:
        return [t("commands.llm.spend.unavailable")]
    try:
        spend = await services.budget.spend()
    except Exception:
        return [t("commands.llm.spend.mongo_down")]
    extra = t("commands.llm.spend.extra", amount=f"{spend.extra_today:.2f}") if spend.extra_today else ""
    return [
        t(
            "commands.llm.spend.today",
            spent=f"{spend.today:.4f}",
            limit=f"{spend.daily_limit(s):.2f}",
            extra=extra,
        ),
        t("commands.llm.spend.month", spent=f"{spend.month:.4f}", limit=f"{s.llm_monthly_budget_usd:.2f}"),
        t(
            "commands.llm.spend.proactive",
            spent=f"{spend.proactive_today:.4f}",
            limit=f"{s.proactive_daily_budget_usd:.2f}",
        ),
        t(
            "commands.llm.spend.selfcode",
            spent=f"{spend.selfcode_today:.4f}",
            limit=f"{s.selfcode_daily_budget_usd:.2f}",
        ),
    ]


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "llm",
        t("commands.llm.llm.description"),
        aliases=("ia", "modelo", "model"),
        examples=("qual modelo voce usa", "qual ia voce usa"),
    )
    async def llm(ctx: CommandContext) -> str:
        services = ctx.services
        s = services.settings
        lines = [t("commands.llm.llm.provider", provider=s.llm_provider.value)]
        if s.llm_base_url:
            lines.append(f"URL: {s.llm_base_url}")
        if services.llm is None:
            lines.append(t("commands.llm.llm.disabled"))
        else:
            lines.append(t("commands.llm.llm.router_model", model=services.llm.router_model))
            lines.append(t("commands.llm.llm.heavy_model", model=services.llm.heavy_model))
            unknown = [
                m
                for m in {services.llm.router_model, services.llm.heavy_model}
                if not services.llm.prices.knows(m)
            ]
            if unknown:
                lines.append(
                    t(
                        "commands.llm.llm.no_price",
                        models=", ".join(sorted(unknown)),
                        input=s.llm_default_price_input,
                        output=s.llm_default_price_output,
                    )
                )
        search = ", ".join(f"{k}={v.name}" for k, v in services.search.items()) or t(
            "commands.llm.llm.no_search"
        )
        lines.append(t("commands.llm.llm.search", search=search))
        lines.extend(await describe_spend(services))
        return "\n".join(lines)
