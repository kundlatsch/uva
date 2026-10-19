"""reminder_create: "me lembra amanhã às 9 de ligar pro dentista" through the LLM."""

from __future__ import annotations

from pydantic import BaseModel, Field

from uva.actions.registry import ActionContext, ActionRegistry, ActionResult


class ReminderCreateParams(BaseModel):
    when: str = Field(
        description=(
            "When, as the user said it ('daqui 20 min', 'amanhã às 9', 'sexta 14h', 'dia 25 às 10h', "
            "'in 2 hours', 'tomorrow 9am') or local time as YYYY-MM-DDTHH:MM"
        )
    )
    text: str = Field(description="What to remind him of, in his words (sent as is, no LLM)")
    request: str = Field(
        default="",
        description="Only when something must be DONE at that time ('vê a previsão do tempo'): the "
        "request, run then like a message; leave empty for a plain reminder",
    )


def register(registry: ActionRegistry) -> None:
    @registry.action(
        "reminder_create",
        "One-shot reminder at a time: sends the text then (or runs the request then)",
        ReminderCreateParams,
    )
    async def reminder_create(params: ReminderCreateParams, ctx: ActionContext) -> ActionResult:
        from uva.reminders import create_reminder

        return ActionResult(
            text=await create_reminder(ctx.services, params.when, text=params.text, request=params.request)
        )
