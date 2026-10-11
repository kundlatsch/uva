"""/decidir: typed answers from the OpenAI Decisions API.

Syntax (also shown in /help):
  /decidir <afirmação>[: texto]                 -> chance de ser verdade
  /decidir <pergunta> | op1, op2, op3 [:: texto] -> escolhe uma opção
  /decidir nota <critério> | n0, n1, ... :: texto -> nota numa escala (níveis em ordem)
A photo sent with the caption "/decidir ..." is used as evidence too.
"""

from __future__ import annotations

from uva.commands.registry import CommandContext, CommandRegistry
from uva.llm.budget import Scope
from uva.llm.decisions import Choice, DecisionsError, Level, Question, format_answers
from uva.media import current_photo

USAGE = (
    "Uso:\n"
    "/decidir <afirmação>[: texto] → chance de ser verdade\n"
    "  ex.: /decidir esse email é spam: Ganhe um iPhone grátis clicando aqui\n"
    "/decidir <pergunta> | op1, op2, op3 [:: texto] → escolhe uma opção\n"
    "  ex.: /decidir o que jantar | pizza, sushi, salada :: tô sem grana e com pressa\n"
    "/decidir nota <critério> | nível0, nível1, ... :: texto → nota de 0 a N\n"
    "  ex.: /decidir nota urgência | baixa, média, alta :: o servidor caiu agora\n"
    "Mande uma foto com a legenda /decidir ... para avaliar a imagem."
)


def parse(text: str, has_photo: bool = False) -> tuple[str, list[Question]]:
    """'/decidir' arguments -> (evidence, questions). Raises ValueError."""
    text = text.strip()
    if not text:
        raise ValueError(USAGE)

    question_part, _, evidence = text.partition("::")
    question_part, evidence = question_part.strip(), evidence.strip()

    if question_part.lower().startswith("nota "):
        criterion, _, levels_text = question_part[5:].partition("|")
        levels = [lv.strip() for lv in levels_text.split(",") if lv.strip()]
        if not criterion.strip() or len(levels) < 2 or not (evidence or has_photo):
            raise ValueError(USAGE)
        question = Question(
            type="score",
            name="nota",
            instructions=f"Avalie: {criterion.strip()}",
            levels=[Level(label=lv) for lv in levels],
        )
        return evidence, [question]

    if "|" in question_part:
        prompt, _, options_text = question_part.partition("|")
        options = [o.strip() for o in options_text.split(",") if o.strip()]
        if not prompt.strip() or len(options) < 2:
            raise ValueError(USAGE)
        question = Question(
            type="choice",
            name="escolha",
            instructions=prompt.strip(),
            choices=[Choice(value=o) for o in options],
        )
        return evidence or prompt.strip(), [question]

    # Predicate: "<afirmação>: texto" (first single colon) or just the statement.
    statement, sep, rest = question_part.partition(":")
    statement = statement.strip()
    if not statement:
        raise ValueError(USAGE)
    evidence = evidence or rest.strip()
    instructions = f"Isto é verdade? {statement}"
    return evidence or statement, [Question(type="predicate", name="afirmacao", instructions=instructions)]


def register(registry: CommandRegistry) -> None:
    @registry.command(
        "decidir",
        "Probabilidade, escolha ou nota com a OpenAI Decisions",
        aliases=("decisao", "decisão"),
        usage="<afirmação>[: texto] | <pergunta> | op1, op2 | nota <critério> | n0, n1 :: texto",
    )
    async def decide(ctx: CommandContext) -> str:
        services = ctx.services
        if services.decisions is None:
            return "A Decisions não está configurada (precisa de uma chave da OpenAI: DECISIONS_API_KEY)."
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
            return f"{services.say('llm_erro')}\n(Decisions: {exc})"
        return format_answers(answers, questions)
