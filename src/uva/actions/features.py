"""feature_request: free-text requests for a new bot command ("cria um comando que...")."""

from __future__ import annotations

from pydantic import BaseModel, Field

from uva.actions.registry import ActionContext, ActionRegistry, ActionResult


class FeatureRequestParams(BaseModel):
    description: str = Field(
        min_length=8,
        description="What the new command should do, with a suggested name and usage examples",
    )


def register(registry: ActionRegistry) -> None:
    @registry.action(
        "feature_request",
        "Programs a new command for the bot (when the user asks for a new feature)",
        FeatureRequestParams,
    )
    async def feature_request(params: FeatureRequestParams, ctx: ActionContext) -> ActionResult:
        from uva.commands.features import request_feature  # avoid an import cycle

        return ActionResult(text=await request_feature(ctx.services, params.description))
