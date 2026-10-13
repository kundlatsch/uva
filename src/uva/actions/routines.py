"""routine_create: "todo dia às 8 pesquisa se ..." through the LLM."""

from __future__ import annotations

from pydantic import BaseModel, Field

from uva.actions.registry import ActionContext, ActionRegistry, ActionResult


class RoutineCreateParams(BaseModel):
    schedule_and_request: str = Field(
        description=(
            "Portuguese, schedule first then the request, in one of these forms: "
            "'todo dia 08:00 <pedido>', 'segunda e quinta 09:00 <pedido>', 'a cada 6 horas <pedido>'"
        )
    )


def register(registry: ActionRegistry) -> None:
    @registry.action(
        "routine_create",
        "Creates a recurring task: a request that runs on a schedule (daily at a time, on weekdays, "
        "or every N hours) and reports to the user",
        RoutineCreateParams,
    )
    async def routine_create(params: RoutineCreateParams, ctx: ActionContext) -> ActionResult:
        from uva.commands.routines import create_routine  # avoid an import cycle

        return ActionResult(text=await create_routine(ctx.services, params.schedule_and_request))
