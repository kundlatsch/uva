"""Fallback tier 2: match free text against command names, aliases and examples."""

from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz

from uva.commands.registry import Command, CommandRegistry
from uva.text import normalize

# Polite/filler words dropped before comparing, so "me mostra o log por favor"
# compares as "log". Only words that never carry the meaning of a request.
FILLER_WORDS = frozenset(
    "por favor pf pfv me mostra mostre mostrar exibe exiba ver veja quero queria "
    "o a os as um uma de do da dos das pra para uva manda mande".split()
)


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
