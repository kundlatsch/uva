"""decide: let the LLM ask the Decisions API instead of guessing numbers."""

from __future__ import annotations

from dataclasses import asdict

from pydantic import BaseModel, Field, field_validator

from uva.actions.registry import ActionContext, ActionRegistry, ActionResult
from uva.i18n import t
from uva.llm.decisions import MAX_QUESTIONS, DecisionsError, Question, format_answers, validate_questions


class DecideParams(BaseModel):
    input: str = Field(default="", description="Evidence to evaluate (text)")
    use_last_photo: bool = Field(default=False, description="Use the last photo the user sent")
    questions: list[Question] = Field(
        description=(
            f"1 to {MAX_QUESTIONS} independent questions: predicate (chance of being true), "
            "choice (one of choices[{value}]) or score (levels[{label}] from lowest to highest). "
            "instructions, choices and levels are shown to the user: write them in the user's language. "
            'E.g.: [{"type": "predicate", "name": "sarcasm", "instructions": "Is the sentence sarcastic?"}, '
            '{"type": "choice", "name": "tone", "instructions": "What is the tone?", "choices": '
            '[{"value": "ironic"}, {"value": "sincere"}]}]'
        )
    )

    @field_validator("questions")
    @classmethod
    def _valid(cls, questions: list[Question]) -> list[Question]:
        validate_questions(questions)
        return questions


def register(registry: ActionRegistry) -> None:
    @registry.action(
        "decide",
        "Answers with calibrated numbers (OpenAI Decisions): the chance of something being "
        "true, a pick among fixed options or a rating on a scale; accepts the last photo",
        DecideParams,
    )
    async def decide(params: DecideParams, ctx: ActionContext) -> ActionResult:
        services = ctx.services
        if services.decisions is None:
            return ActionResult(text=t("actions.decide.not_configured"))
        photo = services.last_photo.get() if params.use_last_photo else None
        if params.use_last_photo and photo is None:
            return ActionResult(text=t("actions.decide.no_photo"))
        if not params.input.strip() and photo is None:
            return ActionResult(text=t("actions.decide.nothing_to_evaluate"))
        try:
            answers = await services.decisions.decide(
                params.input, params.questions, image_data_url=photo, scope=ctx.scope
            )
        except DecisionsError as exc:
            return ActionResult(text=t("actions.decide.failed", error=exc), data={"error": str(exc)})
        return ActionResult(
            text=format_answers(answers, params.questions),
            data={"answers": [asdict(a) for a in answers]},
        )
