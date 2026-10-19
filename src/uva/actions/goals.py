"""goal_create: the LLM turns "fica de olho em X" into a goal for the proactive loop."""

from __future__ import annotations

from pydantic import BaseModel, Field

from uva.actions.registry import ActionContext, ActionRegistry, ActionResult
from uva.goals.store import Goal, GoalKind
from uva.i18n import t
from uva.stm.refs import remember_ref
from uva.logging.events import EventType, emit


class GoalCreateParams(BaseModel):
    title: str = Field(min_length=3, description="Short title of the goal")
    description: str = Field(default="", description="What exactly to do / follow")
    kind: GoalKind = Field(
        default=GoalKind.UNICO,
        description=(
            "unico: a task with an end (create, find out, organize something), finished once "
            "done; recorrente: follow something continuously (news, prices, a topic)"
        ),
    )
    priority: int = Field(default=3, ge=1, le=5, description="1 to 5 (5 = most important)")
    min_interval_hours: float = Field(
        default=6,
        ge=0.25,
        description="Interval between runs in hours (recorrente; e.g. 6 = 4 times a day, 24 = daily)",
    )
    skill: str = Field(default="", description="A skill of yours to follow on every run (optional)")


def register(registry: ActionRegistry) -> None:
    # Not proactive-safe: the loop must not create goals for itself.
    @registry.action(
        "goal_create",
        "Creates a goal for me to work on alone in the background (research, follow, organize notes)",
        GoalCreateParams,
    )
    async def goal_create(params: GoalCreateParams, ctx: ActionContext) -> ActionResult:
        store = ctx.services.goals
        if store is None:
            return ActionResult(text=t("actions.goals.not_configured"))
        goal = Goal(**params.model_dump())
        await store.add(goal)
        await emit(EventType.GOAL_RUN, goal_id=goal.id, title=goal.title, status="created")
        await remember_ref(ctx.services, "objetivo", goal.id, f"goal: {goal.title}")
        return ActionResult(
            text=t("actions.goals.created", id=goal.id, title=goal.title),
            data={"goal_id": goal.id},
        )
