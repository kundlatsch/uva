"""project_create: "cria um projeto novo de ..." through the LLM."""

from __future__ import annotations

from pydantic import BaseModel, Field

from uva.actions.registry import ActionContext, ActionRegistry, ActionResult


class ProjectCreateParams(BaseModel):
    description: str = Field(
        min_length=10,
        description="What the new project should be, with everything the user said (stack, features)",
    )


def register(registry: ActionRegistry) -> None:
    @registry.action(
        "project_create",
        "Creates a NEW software project, separate from the bot, in a new folder, with Claude Code",
        ProjectCreateParams,
    )
    async def project_create(params: ProjectCreateParams, ctx: ActionContext) -> ActionResult:
        from uva.commands.projects import request_project  # avoid an import cycle

        return ActionResult(text=await request_project(ctx.services, params.description))
