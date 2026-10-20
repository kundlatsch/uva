"""Recall: look a message up in every memory system before the LLM answers.

The system prompt only carries the working memory (top N items); the recent
conversation goes along as history. For "me fala sobre o livro X que eu li"
the answer lives elsewhere: an older memory item, a note in the brain, a
goal's results. So before planning, :func:`recall` searches, with no LLM call:

* the long-term memory outside the working set (search index);
* the second brain (title and content, through the search index);
* the goals (any status) and what their runs found;
* the expenses, when the message is about money;
* the conversation log, when the message points to the past ("semana
  passada", "o que eu te falei sobre...").

and returns a block for the message's context with what matched. Without a
search index (tests, SQLite down) memory and notes are scanned directly.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from uva.brain.vault import ARCHIVE_PREFIX, Vault
from uva.clock import format_local, start_of_day, utcnow
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
    the and for with without about what which when where who why how you your yours
    are was were been being have has had did does doing done can could would should will
    tell talk say know remember read saw seen see made make something anything thing things
    book books note notes please hello today yesterday now that this these those there here
    from into just want wanted
    """.split()
)
MONEY_WORDS = frozenset({
    "gasto", "gastos", "gastei", "gastar", "dinheiro", "despesa", "despesas", "paguei", "custo", "reais",
    "spent", "spend", "spending", "money", "expense", "expenses", "paid", "cost", "costs",
})
# Words that only say "in the past" (left out of the conversation-log search).
EPISODE_WORDS = frozenset(
    """
    falei disse contei mencionei comentei falamos conversamos semana passada passado mes
    anteontem dias atras told said mentioned talked last week month ago earlier
    """.split()
)
# (pattern on the normalized text, days back or None for "any time", "yesterday" flag)
_PAST = (
    (re.compile(r"\banteontem\b|\bday before yesterday\b"), "day_before"),
    (re.compile(r"\bontem\b|\byesterday\b"), "yesterday"),
    (re.compile(r"\bsemana passada\b|\blast week\b"), 14),
    (re.compile(r"\bmes passado\b|\blast month\b"), 62),
    (re.compile(r"\bano passado\b|\blast year\b"), 400),
    (re.compile(r"\b(\d+) dias atras\b|\b(\d+) days ago\b"), "days_ago"),
    (re.compile(
        r"\bo que (eu )?(te |lhe )?(falei|disse|contei|mencionei)\b|\b(eu )?(te )?(falei|disse|contei) (que|sobre|de)\b"
        r"|\bwhat did i (tell|say)\b|\bi told you\b"
    ), None),
)

MAX_MEMORY = 8
MAX_NOTES = 4
NOTES_WITH_BODY = 2
NOTE_CHARS = 3000
MAX_GOALS = 3
MAX_TURNS = 10
TURN_CHARS = 300


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
    memory_keys: list[str] = field(default_factory=list)  # items shown (counted as a use)
    turns: int = 0  # conversation-log turns shown
    # Standing intents this message triggered: his own earlier requests, kept apart
    # from the data sections (uva/intents.py).
    intents: str = ""
    intent_ids: list[int] = field(default_factory=list)

    def render(self) -> str:
        return "\n\n".join(self.sections)

    def __bool__(self) -> bool:
        return bool(self.sections or self.intents)


def _needed(words: list[str]) -> int:
    """How many words must match: most of them, so one common word isn't enough."""
    return 1 if len(words) <= 1 else max(2, (len(words) + 1) // 2)


def past_window(text: str, tz, now: datetime | None = None) -> tuple[bool, datetime | None, datetime | None]:
    """(points to the past?, since, until) for messages like "o que eu te
    falei semana passada?"; since/until are None when not bounded."""
    now = now or utcnow()
    flat = normalize(text)
    for pattern, span in _PAST:
        match = pattern.search(flat)
        if not match:
            continue
        today = start_of_day(tz, now)
        if span == "yesterday":
            return True, today - timedelta(days=1), today
        if span == "day_before":
            return True, today - timedelta(days=2), today - timedelta(days=1)
        if span == "days_ago":
            days = int(next(g for g in match.groups() if g))
            return True, today - timedelta(days=days + 1), today - timedelta(days=max(0, days - 1))
        if isinstance(span, int):
            return True, now - timedelta(days=span), now
        return True, None, None
    return False, None, None


async def recall(services: Services, text: str, *, intents: bool = False) -> Recall:
    """``intents``: also check the standing intents (only for the user's own messages)."""
    words = terms(text)
    if past_window(text, services.tz)[0]:
        # "o que eu te falei sobre Lisboa semana passada": look for "lisboa".
        words = [w for w in words if w not in EPISODE_WORDS]
    found = Recall()
    if intents:
        try:
            await _intents(services, text, found)
        except Exception as exc:
            log.warning("recall_failed", step="intents", error=str(exc))
    steps = []
    if words:
        steps += [("memory", _memory), ("notes", _notes), ("goals", _goals), ("expenses", _expenses)]
    for name, step in steps:
        try:
            await step(services, words, found)
        except Exception as exc:  # a store down must not keep the bot from answering
            log.warning("recall_failed", step=name, error=str(exc))
    try:
        await _episodes(services, text, words, found)
    except Exception as exc:
        log.warning("recall_failed", step="episodes", error=str(exc))
    if found.memory_keys and services.memory is not None:
        try:
            await services.memory.touch(found.memory_keys)
        except Exception as exc:
            log.warning("memory_touch_failed", error=str(exc))
    return found


async def _intents(services: Services, text: str, found: Recall) -> None:
    from uva.intents import render_fired, store_for

    store = store_for(services)
    if store is None:
        return
    fired = await store.fire(text)
    if fired:
        found.intents = render_fired(fired, services.tz)
        found.intent_ids = [i.id for i in fired]


async def _memory(services: Services, words: list[str], found: Recall) -> None:
    if services.memory is None:
        return
    working = {i.key for i in await services.memory.working_memory()}
    items = []
    if services.index is not None and await services.index.count("memory"):
        for hit in await services.index.search(words, kinds=("memory",), limit=MAX_MEMORY * 3):
            if hit.ref in working:
                continue  # already in the prompt
            item = await services.memory.get(hit.ref)
            if item is not None:
                items.append(item)
            if len(items) >= MAX_MEMORY:
                break
    else:
        scored = []
        for item, _ in await services.memory.ranked():
            if item.key in working:
                continue
            hits = _matches(words, f"{item.key} {item.value}")
            if hits:
                scored.append((hits, item))
        scored.sort(key=lambda p: -p[0])
        items = [i for _, i in scored[:MAX_MEMORY]]
    lines = [f"- [{i.category.value}] {i.key}: {i.value}" for i in items]
    if lines:
        found.memory_keys += [i.key for i in items]
        found.sections.append("Long-term memory (beyond the working set):\n" + "\n".join(lines))


def _good_enough(words: list[str], title: str, body: str) -> bool:
    need = _needed(words)
    in_title = _matches(words, title)
    in_body = _matches(words, body)
    return max(in_title, in_body) >= need or in_title >= 1 and in_title + in_body >= need + 1


def _score_notes(vault: Vault, words: list[str]) -> list[tuple[int, str]]:
    """Linear scan, used only without a search index."""
    scored = []
    for path, title, body in vault.iter_notes():
        if path.startswith(ARCHIVE_PREFIX):
            continue  # old versions and archived notes: only when asked by name
        if _good_enough(words, title, body):
            scored.append((_matches(words, title) * 3 + _matches(words, body), path))
    scored.sort(key=lambda p: (-p[0], p[1]))
    return scored[:MAX_NOTES]


async def _indexed_notes(services: Services, words: list[str]) -> list[str]:
    assert services.index is not None
    paths = []
    for hit in await services.index.search(words, kinds=("note",), limit=MAX_NOTES * 6):
        if hit.ref.startswith(ARCHIVE_PREFIX) or not _good_enough(words, hit.title, hit.body):
            continue
        paths.append(hit.ref)
        if len(paths) >= MAX_NOTES:
            break
    return paths


async def _notes(services: Services, words: list[str], found: Recall) -> None:
    vault = services.vault
    if vault is None:
        return
    if services.index is not None and await services.index.count("note"):
        paths = await _indexed_notes(services, words)
    else:
        paths = [path for _, path in await asyncio.to_thread(_score_notes, vault, words)]
    blocks = []
    for index, path in enumerate(paths):
        if index < NOTES_WITH_BODY:
            try:
                note = vault.read(path)
                services.note_reads[note.path] = vault.mtime(note.path)  # a later write checks it
            except (LookupError, OSError):
                continue  # removed since it was indexed
            body = note.body.strip()
            if len(body) > NOTE_CHARS:
                body = body[:NOTE_CHARS] + "… (continues; use note_read for the rest)"
            blocks.append(f"### {path}\n{body}")
        else:
            blocks.append(f"### {path} (use note_read to read it)")
        found.notes.append(path)
    if blocks:
        found.sections.append("Second brain notes:\n" + "\n\n".join(blocks))


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
            line += f"\n  result: {result.summary[:600]}" + (f" [note: {result.note}]" if result.note else "")
        lines.append(line)
        if len(lines) >= MAX_GOALS:
            break
    if lines:
        found.sections.append("Related goals:\n" + "\n".join(lines))


async def _expenses(services: Services, words: list[str], found: Recall) -> None:
    if services.expenses is None or not MONEY_WORDS.intersection(words):
        return
    from uva.storage.expenses import format_brl

    recent = await services.expenses.recent(10)
    totals = await services.expenses.totals_by_category()
    lines = [f"- {format_brl(e.amount_cents)} {e.category} {e.description}".rstrip() for e in recent]
    total_lines = [f"- {category}: {format_brl(cents)}" for category, cents in totals]
    found.sections.append(
        "Expenses (latest):\n" + ("\n".join(lines) or "(none)")
        + "\nTotals by category:\n" + ("\n".join(total_lines) or "(none)")
    )


def render_turns(turns, tz) -> str:
    return "\n".join(
        f"- {format_local(turn.at, tz, '%d/%m %H:%M')} [{turn.role}] "
        + (turn.text if len(turn.text) <= TURN_CHARS else turn.text[: TURN_CHARS - 1] + "…")
        for turn in turns
    )


async def _episodes(services: Services, text: str, words: list[str], found: Recall) -> None:
    """Old conversations, only when the message points to the past."""
    if services.index is None:
        return
    past, since, until = past_window(text, services.tz)
    if not past:
        return
    if words:
        turns = await services.index.search_turns(words, since, until, MAX_TURNS)
        turns.sort(key=lambda turn: turn.id)
    elif since is not None:
        turns = await services.index.turns(since, until, MAX_TURNS)
    else:
        return  # "o que eu te falei?" with no topic and no period: nothing to look up
    if turns:
        found.turns = len(turns)
        found.sections.append(
            "Earlier conversations (conversation log, oldest first):\n" + render_turns(turns, services.tz)
        )
