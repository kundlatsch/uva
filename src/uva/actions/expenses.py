"""expense_add: lets "gastei 30 no mercado" be recorded through the LLM."""

from __future__ import annotations

from pydantic import BaseModel, Field

from uva.actions.registry import ActionContext, ActionRegistry, ActionResult
from uva.storage.expenses import format_brl


class ExpenseAddParams(BaseModel):
    amount: float = Field(gt=0, description="Valor em reais (ex.: 25.9)")
    category: str = Field(min_length=1, description="Categoria curta (ex.: mercado, transporte)")
    description: str = Field(default="", description="Detalhe opcional")


def register(registry: ActionRegistry) -> None:
    @registry.action("expense_add", "Registra um gasto do usuário", ExpenseAddParams)
    async def expense_add(params: ExpenseAddParams, ctx: ActionContext) -> ActionResult:
        repo = ctx.services.expenses
        if repo is None:
            return ActionResult(text="O banco SQLite não está configurado.")
        expense = await repo.add(round(params.amount * 100), params.category, params.description)
        return ActionResult(
            text=f"💸 {format_brl(expense.amount_cents)} em {expense.category} registrado (#{expense.id}).",
            data={"id": expense.id},
        )
