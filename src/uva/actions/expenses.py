"""expense_add: lets "gastei 30 no mercado" be recorded through the LLM."""

from __future__ import annotations

from pydantic import BaseModel, Field

from uva.actions.registry import ActionContext, ActionRegistry, ActionResult
from uva.i18n import t
from uva.stm.refs import remember_ref
from uva.storage.expenses import format_brl


class ExpenseAddParams(BaseModel):
    amount: float = Field(gt=0, description="Amount in reais, BRL (e.g. 25.9)")
    category: str = Field(min_length=1, description="Short category (e.g. mercado, transporte)")
    description: str = Field(default="", description="Optional detail")


def register(registry: ActionRegistry) -> None:
    @registry.action("expense_add", "Records an expense of the user", ExpenseAddParams)
    async def expense_add(params: ExpenseAddParams, ctx: ActionContext) -> ActionResult:
        repo = ctx.services.expenses
        if repo is None:
            return ActionResult(text=t("actions.expenses.not_configured"))
        expense = await repo.add(round(params.amount * 100), params.category, params.description)
        await remember_ref(
            ctx.services, "gasto", str(expense.id),
            f"expense: {format_brl(expense.amount_cents)} {expense.category}",
        )
        return ActionResult(
            text=t(
                "actions.expenses.recorded",
                amount=format_brl(expense.amount_cents),
                category=expense.category,
                id=expense.id,
            ),
            data={"id": expense.id},
        )
