"""decide: let the LLM ask the Decisions API instead of guessing numbers."""

from __future__ import annotations

from dataclasses import asdict

from pydantic import BaseModel, Field, field_validator

from uva.actions.registry import ActionContext, ActionRegistry, ActionResult
from uva.i18n import t
from uva.llm.decisions import MAX_QUESTIONS, DecisionsError, Question, format_answers, validate_questions


class DecideParams(BaseModel):
    input: str = Field(default="", description="Evidência a avaliar (texto)")
    use_last_photo: bool = Field(default=False, description="Usar a última foto que o usuário mandou")
    questions: list[Question] = Field(
        description=(
            f"1 a {MAX_QUESTIONS} perguntas independentes: predicate (chance de ser verdade), "
            "choice (uma de choices[{value}]) ou score (levels[{label}] do mais baixo ao mais alto). "
            'Ex.: [{"type": "predicate", "name": "sarcasmo", "instructions": "A frase é sarcástica?"}, '
            '{"type": "choice", "name": "tom", "instructions": "Qual o tom?", "choices": '
            '[{"value": "irônico"}, {"value": "sincero"}]}]'
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
        "Responde com números calibrados (OpenAI Decisions): chance de algo ser verdade, "
        "escolha entre opções fixas ou nota numa escala; aceita a última foto",
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
