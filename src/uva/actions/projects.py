"""project_create: "cria um projeto novo de ..." through the LLM."""

from __future__ import annotations

from pydantic import BaseModel, Field

from uva.actions.registry import ActionContext, ActionRegistry, ActionResult


class ProjectCreateParams(BaseModel):
    description: str = Field(
        min_length=10,
        description="O que o projeto novo deve ser, com tudo que o usuário disse (stack, funções)",
    )


def register(registry: ActionRegistry) -> None:
    @registry.action(
        "project_create",
        "Cria um projeto de software NOVO, separado do bot, numa pasta nova, com o Claude Code",
        ProjectCreateParams,
    )
    async def project_create(params: ProjectCreateParams, ctx: ActionContext) -> ActionResult:
        from uva.commands.projects import request_project  # avoid an import cycle

        return ActionResult(text=await request_project(ctx.services, params.description))
