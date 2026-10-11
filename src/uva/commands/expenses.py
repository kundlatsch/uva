"""/gasto and /gastos: example of structured data in SQLite."""

from __future__ import annotations

from datetime import datetime

from uva.clock import format_local, start_of_day, start_of_month, start_of_week, start_of_year
from uva.commands.registry import CommandContext, CommandRegistry
from uva.services import Services
from uva.storage.expenses import format_brl, parse_amount

NO_DB = "O banco SQLite não está configurado."

PERIODS = {
    "hoje": "hoje",
    "semana": "nesta semana",
    "mes": "neste mês",
    "mês": "neste mês",
    "ano": "neste ano",
    "tudo": "desde sempre",
}


def period_start(name: str, services: Services) -> datetime | None:
    tz = services.tz
    return {
        "hoje": start_of_day(tz),
        "semana": start_of_week(tz),
        "mes": start_of_month(tz),
        "mês": start_of_month(tz),
        "ano": start_of_year(tz),
        "tudo": None,
    }[name]


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "gasto",
        "Registra um gasto",
        aliases=("despesa",),
        usage="<valor> <categoria> [descrição]",
    )
    async def add_expense(ctx: CommandContext) -> str:
        repo = ctx.services.expenses
        if repo is None:
            return NO_DB
        if len(ctx.args) < 2:
            return "Uso: /gasto <valor> <categoria> [descrição] (ex.: /gasto 25,90 mercado pão e leite)"
        try:
            cents = parse_amount(ctx.args[0])
        except ValueError:
            return f"Não entendi o valor '{ctx.args[0]}'. Use algo como 25 ou 25,90."
        expense = await repo.add(cents, ctx.args[1], " ".join(ctx.args[2:]))
        return f"💸 {format_brl(expense.amount_cents)} em {expense.category} registrado (#{expense.id})."

    @registry.command(
        "gastos",
        "Soma dos gastos por categoria",
        aliases=("despesas",),
        usage="[hoje|semana|mes|ano|tudo]",
        examples=("quanto gastei", "quanto gastei esse mes"),
    )
    async def list_expenses(ctx: CommandContext) -> str:
        repo = ctx.services.expenses
        if repo is None:
            return NO_DB
        name = (ctx.args[0].lower() if ctx.args else "mes")
        if name not in PERIODS:
            return "Uso: /gastos [hoje|semana|mes|ano|tudo]"

        totals = await repo.totals_by_category(since=period_start(name, ctx.services))
        if not totals:
            return f"Nenhum gasto {PERIODS[name]}."
        grand = sum(total for _, total in totals)
        lines = [f"Gastos {PERIODS[name]}: {format_brl(grand)}"]
        lines += [
            f"- {category}: {format_brl(total)} ({total * 100 / grand:.0f}%)"
            for category, total in totals
        ]
        recent = await repo.recent(3)
        if recent:
            lines.append("\nÚltimos:")
            lines += [
                f"- {format_local(e.spent_at, ctx.services.tz, '%d/%m')} {format_brl(e.amount_cents)}"
                f" {e.category}{' · ' + e.description if e.description else ''}"
                for e in recent
            ]
        return "\n".join(lines)
