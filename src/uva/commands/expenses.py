"""/gasto and /gastos: example of structured data in SQLite."""

from __future__ import annotations

from datetime import datetime

from uva.clock import format_local, start_of_day, start_of_month, start_of_week, start_of_year
from uva.commands.registry import CommandContext, CommandRegistry
from uva.i18n import t
from uva.stm.refs import remember_ref
from uva.services import Services
from uva.storage.expenses import format_brl, parse_amount

PERIODS = ("hoje", "semana", "mes", "mês", "ano", "tudo")
# English period names accepted by /gastos (and /expenses).
PERIOD_ALIASES = {"today": "hoje", "week": "semana", "month": "mes", "year": "ano", "all": "tudo"}


def period_label(name: str) -> str:
    """'mes' -> 'neste mês' / 'this month'."""
    return t(f"commands.expenses.period.{'mes' if name == 'mês' else name}")


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
        t("commands.expenses.gasto.description"),
        aliases=("despesa", "expense"),
        usage=t("commands.expenses.gasto.usage_arg"),
    )
    async def add_expense(ctx: CommandContext) -> str:
        repo = ctx.services.expenses
        if repo is None:
            return t("commands.expenses.no_db")
        if len(ctx.args) < 2:
            return t("commands.expenses.gasto.usage")
        try:
            cents = parse_amount(ctx.args[0])
        except ValueError:
            return t("commands.expenses.gasto.bad_amount", value=ctx.args[0])
        expense = await repo.add(cents, ctx.args[1], " ".join(ctx.args[2:]))
        await remember_ref(
            ctx.services, "gasto", str(expense.id), f"expense: {format_brl(cents)} {expense.category}"
        )
        return t(
            "commands.expenses.gasto.saved",
            amount=format_brl(expense.amount_cents),
            category=expense.category,
            id=expense.id,
        )

    @registry.command(
        "gastos",
        t("commands.expenses.gastos.description"),
        aliases=("despesas", "expenses"),
        usage=t("commands.expenses.gastos.usage_arg"),
        examples=("quanto gastei", "quanto gastei esse mes"),
    )
    async def list_expenses(ctx: CommandContext) -> str:
        repo = ctx.services.expenses
        if repo is None:
            return t("commands.expenses.no_db")
        name = (ctx.args[0].lower() if ctx.args else "mes")
        name = PERIOD_ALIASES.get(name, name)
        if name not in PERIODS:
            return t("commands.expenses.gastos.usage")

        totals = await repo.totals_by_category(since=period_start(name, ctx.services))
        if not totals:
            return t("commands.expenses.gastos.none", period=period_label(name))
        grand = sum(total for _, total in totals)
        lines = [t("commands.expenses.gastos.total", period=period_label(name), total=format_brl(grand))]
        lines += [
            f"- {category}: {format_brl(total)} ({total * 100 / grand:.0f}%)"
            for category, total in totals
        ]
        recent = await repo.recent(3)
        if recent:
            lines.append(t("commands.expenses.gastos.recent"))
            lines += [
                f"- {format_local(e.spent_at, ctx.services.tz, '%d/%m')} {format_brl(e.amount_cents)}"
                f" {e.category}{' · ' + e.description if e.description else ''}"
                for e in recent
            ]
        return "\n".join(lines)
