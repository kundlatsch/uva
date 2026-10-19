"""ask: a question with 2-4 options as buttons (uva/questions.py)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from uva.actions.registry import ActionContext, ActionRegistry, ActionResult
from uva.i18n import t


class AskParams(BaseModel):
    question: str = Field(description="The question, in the user's language")
    options: list[str] = Field(min_length=2, max_length=4, description="2 to 4 short answers (button labels)")
    context: str = Field(default="", description="What you will do with the answer (for yourself, in English)")


def register(registry: ActionRegistry) -> None:
    @registry.action(
        "ask",
        "Asks the user a question with 2-4 answer buttons; his choice comes back as his next message",
        AskParams,
    )
    async def ask(params: AskParams, ctx: ActionContext) -> ActionResult:
        from uva import questions

        store = questions.store_for(ctx.services)
        if store is None:
            return ActionResult(text=t("ask.no_storage"))
        try:
            question = await store.create(params.question, params.options, context=params.context)
        except ValueError:
            return ActionResult(text=t("ask.no_options"))
        return ActionResult(
            text=question.question, buttons=questions.buttons(question), data={"question_id": question.id},
        )
