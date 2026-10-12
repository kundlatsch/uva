"""Recall: look a message up in every memory system before the LLM answers.

The system prompt only carries the working memory (top N items) and the
recent conversation. For "me fala sobre o livro X que eu li" the answer lives
elsewhere: an older memory item, a note in the brain, a goal's results. So
before planning, :func:`recall` searches, with no LLM call:

* the whole long-term memory (not only the working set);
* the second brain (title and content, all the words of the message);
* the goals (any status) and what their runs found;
* the expenses, when the message is about money.

and returns a block for the system prompt with what matched.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from uva.brain.vault import ARCHIVE_PREFIX, Vault
from uva.logging import get_logger
from uva.services import Services
from uva.text import normalize

log = get_logger(__name__)

# Words that say nothing about what to look for.
STOPWORDS = frozenset(
    """
    a o as os um uma uns umas de do da dos das no na nos nas em por para pra pro com sem
    que se e ou mas eu me mim meu minha meus minhas voce vc te ti seu sua seus suas ele ela
    eles elas isso isto esse essa este esta aquele aquela aquilo ai aqui la ja nao sim
    fala falar fale diga dizer conta contar conte sobre sabe saber lembra lembrar lembre
    qual quais quando onde como quem porque pq quero queria pode poderia tem tinha ter foi
    era sao ser estar esta estou li ler lido leu vi ver visto fiz fazer feito tudo todo toda
    algo alguma algum coisa coisas livro livros nota notas favor oi ola hoje ontem agora
    """.split()
)
MONEY_WORDS = frozenset({"gasto", "gastos", "gastei", "gastar", "dinheiro", "despesa", "despesas", "paguei", "custo", "reais"})

MAX_MEMORY = 8
MAX_NOTES = 4
NOTES_WITH_BODY = 2
NOTE_CHARS = 3000
MAX_GOALS = 3


def terms(text: str) -> list[str]:
    """Distinct meaningful words of ``text`` (normalized, in order)."""
    out: list[str] = []
    for word in normalize(text).split():
        if len(word) >= 3 and word not in STOPWORDS and word not in out:
            out.append(word)
    return out


def _matches(words: list[str], haystack: str) -> int:
    hay = f" {normalize(haystack)} "
    return sum(1 for w in words if f" {w}" in hay)


@dataclass
class Recall:
    sections: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # paths shown, for the log

    def render(self) -> str:
        return "\n\n".join(self.sections)

    def __bool__(self) -> bool:
        return bool(self.sections)


def _needed(words: list[str]) -> int:
    """How many words must match: most of them, so one common word isn't enough."""
    return 1 if len(words) <= 1 else max(2, (len(words) + 1) // 2)


async def recall(services: Services, text: str) -> Recall:
    words = terms(text)
    found = Recall()
    if not words:
        return found
    for name, step in (("memory", _memory), ("notes", _notes), ("goals", _goals), ("expenses", _expenses)):
        try:
            await step(services, words, found)
        except Exception as exc:  # a store down must not keep the bot from answering
            log.warning("recall_failed", step=name, error=str(exc))
    return found


async def _memory(services: Services, words: list[str], found: Recall) -> None:
    if services.memory is None:
        return
    working = {i.key for i in await services.memory.working_memory()}
    scored = []
    for item, _ in await services.memory.ranked():
        if item.key in working:
            continue  # already in the prompt
        hits = _matches(words, f"{item.key} {item.value}")
        if hits:
            scored.append((hits, item))
    scored.sort(key=lambda p: -p[0])
    lines = [f"- [{i.category.value}] {i.key}: {i.value}" for _, i in scored[:MAX_MEMORY]]
    if lines:
        found.sections.append("Memória longa (além da de trabalho):\n" + "\n".join(lines))


def _score_notes(vault: Vault, words: list[str]) -> list[tuple[int, str]]:
    need = _needed(words)
    scored = []
    for path, title, body in vault.iter_notes():
        if path.startswith(ARCHIVE_PREFIX):
            continue  # old versions and archived notes: only when asked by name
        in_title = _matches(words, title)
        in_body = _matches(words, body)
        if max(in_title, in_body) >= need or in_title >= 1 and in_title + in_body >= need + 1:
            scored.append((in_title * 3 + in_body, path))
    scored.sort(key=lambda p: (-p[0], p[1]))
    return scored[:MAX_NOTES]


async def _notes(services: Services, words: list[str], found: Recall) -> None:
    vault = services.vault
    if vault is None:
        return
    scored = await asyncio.to_thread(_score_notes, vault, words)
    if not scored:
        return
    blocks = []
    for index, (_, path) in enumerate(scored):
        if index < NOTES_WITH_BODY:
            note = vault.read(path)
            body = note.body.strip()
            if len(body) > NOTE_CHARS:
                body = body[:NOTE_CHARS] + "… (continua; use note_read para o resto)"
            blocks.append(f"### {path}\n{body}")
        else:
            blocks.append(f"### {path} (use note_read para ler)")
        found.notes.append(path)
    found.sections.append("Notas do second brain:\n" + "\n\n".join(blocks))


async def _goals(services: Services, words: list[str], found: Recall) -> None:
    if services.goals is None:
        return
    need = _needed(words)
    lines = []
    for goal in await services.goals.list():
        results = " ".join(r.summary for r in goal.results[-3:])
        if _matches(words, f"{goal.title} {goal.description} {results}") < need:
            continue
        line = f"- {goal.title} ({goal.status.value})"
        for result in goal.results[-2:]:
            line += f"\n  resultado: {result.summary[:600]}" + (f" [nota: {result.note}]" if result.note else "")
        lines.append(line)
        if len(lines) >= MAX_GOALS:
            break
    if lines:
        found.sections.append("Objetivos relacionados:\n" + "\n".join(lines))


async def _expenses(services: Services, words: list[str], found: Recall) -> None:
    if services.expenses is None or not MONEY_WORDS.intersection(words):
        return
    from uva.storage.expenses import format_brl

    recent = await services.expenses.recent(10)
    totals = await services.expenses.totals_by_category()
    lines = [f"- {format_brl(e.amount_cents)} {e.category} {e.description}".rstrip() for e in recent]
    total_lines = [f"- {category}: {format_brl(cents)}" for category, cents in totals]
    found.sections.append(
        "Gastos (últimos):\n" + ("\n".join(lines) or "(nenhum)")
        + "\nTotais por categoria:\n" + ("\n".join(total_lines) or "(nenhum)")
    )
