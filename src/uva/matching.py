"""Fallback tier 2: match free text against command names, aliases and examples."""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from uva.commands.registry import Command, CommandRegistry
from uva.text import normalize

# Polite/filler words dropped before comparing, so "me mostra o log por favor"
# (or "show me the log please") compares as "log". Only words that never carry
# the meaning of a request. Portuguese first, then English.
FILLER_WORDS = frozenset(
    "por favor pf pfv me mostra mostre mostrar exibe exiba ver veja quero queria "
    "o a os as um uma de do da dos das pra para uva manda mande "
    "please pls plz show display view see want wanna can could you i the an of to my "
    "give send".split()
)


# Short social messages answered from the phrases file, without the LLM.
SMALL_TALK: dict[str, tuple[str, ...]] = {
    "saudacao": (
        "oi", "ola", "oie", "opa", "e ai", "eai", "fala", "salve", "bom dia", "boa tarde",
        "boa noite", "hey", "hello", "oi uva", "ola uva", "fala uva", "e ai uva", "tudo bem",
        "tudo bom", "beleza", "suave",
        # English
        "hi", "hiya", "hi uva", "hello uva", "hey uva", "hey there", "yo", "good morning",
        "good afternoon", "good evening", "how are you", "how are you doing", "whats up",
        "what s up", "sup",
    ),
    "agradecimento": (
        "valeu", "vlw", "obrigado", "obrigada", "brigado", "brigada", "obg", "thanks",
        "muito obrigado", "valeu uva", "obrigado uva", "show", "massa", "top",
        # English
        "thank you", "thanks uva", "thank you uva", "thank you so much", "thanks a lot",
        "thx", "ty", "cheers", "awesome", "great", "nice", "cool",
    ),
    "despedida": (
        "tchau", "falou", "flw", "ate mais", "ate logo", "ate amanha", "fui", "bye",
        "tchau uva", "falou uva", "boa noite uva", "ate",
        # English
        "goodbye", "good bye", "bye uva", "bye bye", "see you", "see ya", "see you later",
        "see you tomorrow", "later", "cya", "good night", "good night uva", "gotta go",
    ),
}
SMALL_TALK_MAX_WORDS = 4


def small_talk(text: str) -> str | None:
    """Category of a short greeting/thanks/goodbye, or None."""
    normalized = normalize(text)
    if not normalized or len(normalized.split()) > SMALL_TALK_MAX_WORDS:
        return None
    for category, phrases in SMALL_TALK.items():
        if normalized in phrases:
            return category
    # Tolerate typos and repeated letters ("valeuuu", "obrigadoo").
    squeezed = re.sub(r"(.)\1+", r"\1", normalized)
    best = max(
        ((fuzz.ratio(squeezed, p), c) for c, ps in SMALL_TALK.items() for p in ps), default=(0, None)
    )
    return best[1] if best[0] >= 90 else None


# "como tá a tarefa?", "e a pesquisa?", "deu certo?" (and "how is the task?",
# "did it work?"). The values are the reference kinds stored in the session.
STATUS_KINDS = {
    "tarefa": "tarefa", "tarefas": "tarefa", "task": "tarefa",
    "pesquisa": "pesquisa", "busca": "pesquisa", "pesquisas": "pesquisa",
    "feature": "feature", "funcionalidade": "feature", "comando": "feature",
    "objetivo": "objetivo", "meta": "objetivo",
    # English ("search" is left out: "search X" is a request, not a question)
    "tasks": "tarefa", "job": "tarefa", "research": "pesquisa", "features": "feature",
    "command": "feature", "goal": "objetivo",
}
STATUS_CUES = (
    "como ta", "como esta", "como anda", "como foi", "e a", "e o", "deu certo", "terminou",
    "acabou", "ja foi", "status", "andamento", "ficou pronta", "ficou pronto", "ta pronta",
    "ta pronto", "funcionou", "rodou", "e ai", "noticia",
    # English
    "how is", "how s", "hows", "how was", "how did", "how about", "what about", "and the",
    "did it work", "did it finish", "is it done", "done yet", "finished", "is it ready",
    "ready yet", "progress", "worked", "any news", "news",
)
STATUS_EXACT = {
    "deu certo", "terminou", "acabou", "ja foi", "funcionou", "rodou", "e ai deu certo",
    "ja terminou", "ja acabou", "ficou pronto", "ficou pronta", "e entao", "e ai como ta",
    # English
    "did it work", "did it finish", "is it done", "is it finished", "done yet", "finished",
    "is it ready", "any news", "and then", "did it run", "it worked",
}
REFERENTS = {"aquela", "aquele", "ela", "ele", "isso", "essa", "esse", "it", "that", "this"}
STATUS_MAX_WORDS = 9


@dataclass(frozen=True, slots=True)
class StatusQuestion:
    kind: str | None  # "pesquisa", "tarefa"... or None for "whatever is in focus"


def status_question(text: str) -> StatusQuestion | None:
    """Short questions about how something the bot did is going."""
    normalized = normalize(text)
    words = normalized.split()
    if not words or len(words) > STATUS_MAX_WORDS:
        return None
    if normalized in STATUS_EXACT:
        return StatusQuestion(None)
    padded = f" {normalized} "
    has_cue = any(f" {cue} " in padded for cue in STATUS_CUES)
    kind = next((STATUS_KINDS[w] for w in words if w in STATUS_KINDS), None)
    if kind and (has_cue or len(words) <= 3):
        return StatusQuestion(kind)
    if has_cue and REFERENTS & set(words):
        return StatusQuestion(None)
    return None


def strip_filler(normalized: str) -> str:
    return " ".join(w for w in normalized.split() if w not in FILLER_WORDS)


@dataclass(frozen=True, slots=True)
class Match:
    command: Command
    score: float  # 0-100
    args: list[str]
    phrase: str  # the alias/example that matched best


def score(text: str, phrase: str) -> float:
    # token_sort tolerates word order; plain ratio handles typos in short text.
    return max(fuzz.ratio(text, phrase), fuzz.token_sort_ratio(text, phrase))


def best_match(text: str, registry: CommandRegistry) -> Match | None:
    """Best scoring command for ``text``, or None if nothing is comparable."""
    normalized = normalize(text)
    best: Match | None = None

    for command in registry:
        if command.extract_args is not None:
            remaining, args = command.extract_args(normalized)
        else:
            remaining, args = normalized, []
        candidate = strip_filler(remaining)
        if not candidate:
            continue

        for phrase in (command.name, *command.aliases, *command.examples):
            target = strip_filler(normalize(phrase)) or normalize(phrase)
            value = score(candidate, target)
            if best is None or value > best.score:
                best = Match(command, value, args, phrase)

    return best
