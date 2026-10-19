"""/decidir: typed answers from the OpenAI Decisions API.

Syntax (also shown in /help):
  /decidir <afirmação>[: texto]                 -> chance de ser verdade
  /decidir <pergunta> | op1, op2, op3 [:: texto] -> escolhe uma opção
  /decidir nota <critério> | n0, n1, ... :: texto -> nota numa escala (níveis em ordem)
A photo sent with the caption "/decidir ..." is used as evidence too.
"""

from __future__ import annotations

from uva.commands.registry import CommandContext, CommandRegistry
from uva.i18n import t
from uva.llm.budget import Scope
from uva.llm.decisions import Choice, DecisionsError, Level, Question, format_answers
from uva.media import current_photo

# "nota" in Portuguese, "score" in English.
SCORE_PREFIXES = ("nota ", "score ")


def usage() -> str:
    return t("commands.decide.usage")


def parse(text: str, has_photo: bool = False) -> tuple[str, list[Question]]:
    """'/decidir' arguments -> (evidence, questions). Raises ValueError."""
    text = text.strip()
    if not text:
        raise ValueError(usage())

    question_part, _, evidence = text.partition("::")
    question_part, evidence = question_part.strip(), evidence.strip()

    prefix = next((p for p in SCORE_PREFIXES if question_part.lower().startswith(p)), None)
    if prefix is not None:
        criterion, _, levels_text = question_part[len(prefix):].partition("|")
        levels = [lv.strip() for lv in levels_text.split(",") if lv.strip()]
        if not criterion.strip() or len(levels) < 2 or not (evidence or has_photo):
            raise ValueError(usage())
        question = Question(
            type="score",
            name="score",
            instructions=f"Rate: {criterion.strip()}",
            title=t("commands.decide.title.score", criterion=criterion.strip()),
            levels=[Level(label=lv) for lv in levels],
        )
        return evidence, [question]

    if "|" in question_part:
        prompt, _, options_text = question_part.partition("|")
        options = [o.strip() for o in options_text.split(",") if o.strip()]
        if not prompt.strip() or len(options) < 2:
            raise ValueError(usage())
        question = Question(
            type="choice",
            name="choice",
            instructions=prompt.strip(),
            choices=[Choice(value=o) for o in options],
        )
        return evidence or prompt.strip(), [question]

    # Predicate: "<afirmação>: texto" (first single colon) or just the statement.
    statement, sep, rest = question_part.partition(":")
    statement = statement.strip()
    if not statement:
        raise ValueError(usage())
    evidence = evidence or rest.strip()
    question = Question(
        type="predicate",
        name="statement",
        instructions=f"Is this true? {statement}",
        title=t("commands.decide.title.predicate", statement=statement),
    )
    return evidence or statement, [question]


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "decidir",
        t("commands.decide.decidir.description"),
        aliases=("decisao", "decisão", "decide", "decision"),
        usage=t("commands.decide.decidir.usage_arg"),
    )
    async def decide(ctx: CommandContext) -> str:
        services = ctx.services
        if services.decisions is None:
            return t("commands.decide.decidir.not_configured")
        photo = current_photo.get()
        try:
            evidence, questions = parse(" ".join(ctx.args), has_photo=photo is not None)
        except ValueError as exc:
            return str(exc)
        if services.budget is not None:
            status = await services.budget.check(Scope.INTERACTIVE)
            if not status.allowed:
                return f"{services.say('sem_credito')}\n({status.reason})"
        try:
            answers = await services.decisions.decide(evidence, questions, image_data_url=photo)
        except DecisionsError as exc:
            return t("commands.decide.decisions_error", phrase=services.say("llm_erro"), error=exc)
        return format_answers(answers, questions)
