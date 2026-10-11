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


_DURATION = re.compile(
    r"\b(?:(?:ultim[oa]s?|nas?|na)\s+)?(\d+|uma?|meia)\s*"
    r"(m|min|mins|minutos?|h|hr|hrs|horas?|d|dias?)\b"
)
_SINGLE_UNIT = re.compile(r"\bultim[oa]\s+(hora|dia|minuto)\b")
_UNIT_CODE = {"m": "m", "h": "h", "d": "d"}


def extract_duration(normalized: str) -> tuple[str, str | None]:
    """Find "2 horas", "30 min", "ultimo dia"... -> (text without it, "2h")."""
    if match := _DURATION.search(normalized):
        raw_amount, unit = match[1], match[2]
        if raw_amount == "meia":
            amount, code = 30, "m"
        else:
            amount = 1 if raw_amount in ("um", "uma") else int(raw_amount)
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
