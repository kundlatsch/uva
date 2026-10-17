"""Text normalization and simple argument extraction from free text."""

from __future__ import annotations

import re
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize(text: str) -> str:
    """Lowercase, strip accents and punctuation, collapse spaces."""
    decomposed = unicodedata.normalize("NFKD", text)
    no_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _NON_ALNUM.sub(" ", no_accents.lower()).strip()


# Portuguese and English: "ultimas 2 horas", "last 2 hours", "30 min", "an hour".
_DURATION = re.compile(
    r"\b(?:(?:ultim[oa]s?|nas?|na|(?:in\s+)?the\s+last|last|past)\s+)?(\d+|uma?|meia|an|one)\s*"
    r"(m|min|mins|minutos?|minutes?|h|hr|hrs|horas?|hours?|d|dias?|days?)\b"
)
_SINGLE_UNIT = re.compile(r"\b(?:ultim[oa]|last|past)\s+(hora|dia|minuto|hour|day|minute)\b")
_UNIT_CODE = {"m": "m", "h": "h", "d": "d"}


def extract_duration(normalized: str) -> tuple[str, str | None]:
    """Find "2 horas", "30 min", "ultimo dia", "last 2 hours"... -> (text without it, "2h")."""
    if match := _DURATION.search(normalized):
        raw_amount, unit = match[1], match[2]
        if raw_amount == "meia":
            amount, code = 30, "m"
        else:
            amount = 1 if raw_amount in ("um", "uma", "an", "one") else int(raw_amount)
            code = _UNIT_CODE["m" if unit.startswith("m") else unit[0]]
        return _cut(normalized, match), f"{amount}{code}"
    if match := _SINGLE_UNIT.search(normalized):
        return _cut(normalized, match), f"1{match[1][0]}"
    return normalized, None


def extract_keywords(normalized: str, keywords: dict[str, str]) -> tuple[str, list[str]]:
    """Pull out known words, mapping each to an argument (e.g. "erro" -> "erros")."""
    found: list[str] = []
    kept: list[str] = []
    for word in normalized.split():
        if word in keywords:
            if keywords[word] not in found:
                found.append(keywords[word])
        else:
            kept.append(word)
    return " ".join(kept), found


def _cut(text: str, match: re.Match[str]) -> str:
    return " ".join((text[: match.start()] + " " + text[match.end() :]).split())
