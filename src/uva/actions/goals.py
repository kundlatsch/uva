"""goal_create: the LLM turns "fica de olho em X" into a goal for the proactive loop."""

from __future__ import annotations

from pydantic import BaseModel, Field

from uva.actions.registry import ActionContext, ActionRegistry, ActionResult
from uva.goals.store import Goal, GoalKind
from uva.stm.refs import remember_ref
from uva.logging.events import EventType, emit


class GoalCreateParams(BaseModel):
    title: str = Field(min_length=3, description="Título curto do objetivo")
    description: str = Field(default="", description="O que exatamente fazer / acompanhar")
    kind: GoalKind = Field(
        default=GoalKind.RECORRENTE,
        description="recorrente (repete a cada intervalo) ou unico (termina quando concluído)",
    )
    priority: int = Field(default=3, ge=1, le=5, description="1 a 5 (5 = mais importante)")
    min_interval_hours: float = Field(
        default=24, ge=1, description="Intervalo mínimo entre execuções, em horas"
    )


def register(registry: ActionRegistry) -> None:
    # Not proactive-safe: the loop must not create goals for itself.
    @registry.action(
        "goal_create",
        "Cria um objetivo para eu trabalhar sozinha em segundo plano (pesquisar, acompanhar, organizar notas)",
        GoalCreateParams,
    )
    async def goal_create(params: GoalCreateParams, ctx: ActionContext) -> ActionResult:
        store = ctx.services.goals
        if store is None:
            return ActionResult(text="Objetivos não estão configurados.")
        goal = Goal(**params.model_dump())
        await store.add(goal)
        await emit(EventType.GOAL_RUN, goal_id=goal.id, title=goal.title, status="created")
        await remember_ref(ctx.services, "objetivo", goal.id, f"objetivo: {goal.title}")
        return ActionResult(
            text=f"🎯 Objetivo criado ({goal.id}): {goal.title}. Veja em /objetivos.",
            data={"goal_id": goal.id},
        )
