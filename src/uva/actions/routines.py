"""routine_create: "every day at 8 check whether ..." through the LLM."""

from __future__ import annotations

from pydantic import BaseModel, Field

from uva.actions.registry import ActionContext, ActionRegistry, ActionResult


class RoutineCreateParams(BaseModel):
    schedule_and_request: str = Field(
        description=(
            "Schedule first, then the request, in one of these forms (English or Portuguese): "
            "'every day 08:00 <request>', 'monday and thursday 09:00 <request>', "
            "'every 6 hours <request>' / 'todo dia 08:00 <pedido>', 'segunda e quinta 09:00 <pedido>', "
            "'a cada 6 horas <pedido>'. Keep the request in the user's own words."
        )
    )
    skill: str = Field(default="", description="A skill of yours to follow on every run (optional)")
    script: str = Field(
        default="",
        description="Script-only routine: a file in /uva (e.g. 'checks/site.sh') run without the LLM; "
        "its output is sent, empty output sends nothing",
    )
    context_from: str = Field(default="", description="Id of another routine whose last output goes along")


def register(registry: ActionRegistry) -> None:
    @registry.action(
        "routine_create",
        "Creates a recurring task: a request that runs on a schedule (daily at a time, on weekdays, "
        "or every N hours) and reports to the user",
        RoutineCreateParams,
    )
    async def routine_create(params: RoutineCreateParams, ctx: ActionContext) -> ActionResult:
        from uva.commands.routines import create_routine  # avoid an import cycle

        return ActionResult(
            text=await create_routine(
                ctx.services, params.schedule_and_request, skill=params.skill,
                script=params.script, context_from=params.context_from,
            )
        )
