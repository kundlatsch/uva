"""intent_create / intent_cancel: "when X comes up, do Y" (uva/intents.py)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from uva.actions.registry import ActionContext, ActionRegistry, ActionResult
from uva.i18n import t
from uva.logging.events import EventType, current_trace_id, emit


class IntentCreateParams(BaseModel):
    text: str = Field(description="What to do when it comes up, in his words (e.g. 'lembrar do passaporte')")
    triggers: list[list[str] | str] = Field(
        description="Entries; each entry is a list of terms that must ALL appear in a message "
        "(e.g. [['viagem'], ['passaporte', 'vencendo']]); a string is one term"
    )
    max_fires: int = Field(default=3, ge=1, le=50, description="How many times it may fire")
    cooldown_hours: float = Field(default=24, ge=0, description="Hours before it can fire again")
    expires_days: float = Field(default=90, ge=1, le=3650, description="Days until it expires")
    scope: Literal["messages", "results"] = Field(
        default="messages", description="messages: his messages; results: research and goal results"
    )


class IntentCancelParams(BaseModel):
    id: int = Field(description="The intent's number (/intencoes)")


def register(registry: ActionRegistry) -> None:
    @registry.action(
        "intent_create",
        "Standing intent: when terms come up in his messages (or in results), do something",
        IntentCreateParams,
    )
    async def intent_create(params: IntentCreateParams, ctx: ActionContext) -> ActionResult:
        from uva.intents import store_for

        store = store_for(ctx.services)
        if store is None:
            return ActionResult(text=t("intents.no_storage"))
        try:
            intent = await store.create(
                params.text, params.triggers, max_fires=params.max_fires, cooldown_hours=params.cooldown_hours,
                expires_days=params.expires_days, scope=params.scope, source_trace=current_trace_id(),
            )
        except ValueError:
            return ActionResult(text=t("intents.no_triggers"))
        await emit(EventType.MEMORY, op="intent_create", id=intent.id, triggers=intent.triggers)
        key = "intents.created_results" if intent.scope == "results" else "intents.created"
        return ActionResult(
            text=t(key, id=intent.id, text=intent.text, triggers=intent.describe_triggers()),
            data={"id": intent.id},
        )

    @registry.action("intent_cancel", "Cancels a standing intent (only when he asks)", IntentCancelParams)
    async def intent_cancel(params: IntentCancelParams, ctx: ActionContext) -> ActionResult:
        from uva.intents import store_for

        store = store_for(ctx.services)
        if store is None:
            return ActionResult(text=t("intents.no_storage"))
        intent = await store.cancel(params.id)
        if intent is None:
            return ActionResult(text=t("intents.not_found", id=params.id))
        await emit(EventType.MEMORY, op="intent_cancel", id=intent.id)
        return ActionResult(text=t("intents.cancelled", id=intent.id, text=intent.text))
