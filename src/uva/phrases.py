"""Ready-made phrases in the bot's voice (data/fallback_phrases.json).

Used whenever an answer doesn't need (or can't use) the LLM: budget out,
LLM errors, "não entendi", greetings, task notices... The file is validated
on load and re-read when its mtime changes; if it is missing or invalid, a
neutral built-in phrase is used so the bot never breaks over it.
"""

from __future__ import annotations

import json
import random
import string
from collections import deque
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from uva.i18n import t
from uva.logging import get_logger

log = get_logger(__name__)

MAX_PHRASE_CHARS = 300
RECENT_PER_CATEGORY = 3

# Categories with a neutral phrase (in the current language, from
# locales/<lang>/phrases.json), used when the file is unavailable or lacks one.
BUILTIN_CATEGORIES = (
    "sem_credito", "llm_erro", "nao_entendi", "ocupado", "tarefa_falhou", "tarefa_ok",
    "pesquisando", "saudacao", "agradecimento", "despedida", "confirmacao", "cancelado",
    "sem_resultado", "erro_generico", "transcrevendo", "comando_desconhecido", "pensando",
    "executando", "programando", "procurando", "olhando", "salvando",
)


class _Builtin(Mapping[str, str]):
    """BUILTIN["saudacao"] -> the neutral phrase in the current language."""

    def __getitem__(self, category: str) -> str:
        if category not in BUILTIN_CATEGORIES:
            raise KeyError(category)
        return t(f"phrases.builtin.{category}")

    def __iter__(self) -> Iterator[str]:
        return iter(BUILTIN_CATEGORIES)

    def __len__(self) -> int:
        return len(BUILTIN_CATEGORIES)


BUILTIN: Mapping[str, str] = _Builtin()


class PhrasesError(ValueError):
    pass


def placeholders(phrase: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(phrase) if name}


def validate(data: Any) -> dict[str, list[str]]:
    """Check the file structure; returns the categories."""
    if not isinstance(data, dict) or not isinstance(data.get("version"), int):
        raise PhrasesError(t("phrases.errors.structure"))
    documented = data.get("placeholders", {})
    if not isinstance(documented, dict):
        raise PhrasesError(t("phrases.errors.placeholders"))
    categories = data.get("categories")
    if not isinstance(categories, dict) or not categories:
        raise PhrasesError(t("phrases.errors.categories"))
    for name, phrases in categories.items():
        if not isinstance(phrases, list) or not phrases:
            raise PhrasesError(t("phrases.errors.category", name=repr(name)))
        for phrase in phrases:
            if not isinstance(phrase, str) or not phrase.strip():
                raise PhrasesError(t("phrases.errors.empty_phrase", name=repr(name)))
            if len(phrase) > MAX_PHRASE_CHARS:
                raise PhrasesError(t("phrases.errors.too_long", name=repr(name), start=phrase[:40]))
            try:
                unknown = placeholders(phrase) - set(documented)
            except ValueError as exc:
                raise PhrasesError(t("phrases.errors.malformed", name=repr(name), phrase=phrase)) from exc
            if unknown:
                raise PhrasesError(t("phrases.errors.undocumented", unknown=unknown, name=repr(name)))
    return categories


class Phrases:
    def __init__(self, path: Path | None, rng: random.Random | None = None) -> None:
        self.path = path
        self.rng = rng or random.Random()
        self._mtime: float | None = None
        self._categories: dict[str, list[str]] = {}
        self._recent: dict[str, deque[int]] = {}

    @property
    def categories(self) -> dict[str, list[str]]:
        self._refresh()
        return self._categories

    def pick(self, category: str, **values: Any) -> str:
        """A random phrase of ``category`` (not one of the last few), filled with ``values``.

        Phrases needing a placeholder that wasn't given are skipped.
        """
        options = self.categories.get(category, [])
        usable = [i for i, p in enumerate(options) if placeholders(p) <= values.keys()]
        if not usable:
            fallback = BUILTIN.get(category, BUILTIN["erro_generico"])
            return fallback.format(**values) if placeholders(fallback) <= values.keys() else fallback

        recent = self._recent.setdefault(category, deque(maxlen=RECENT_PER_CATEGORY))
        fresh = [i for i in usable if i not in recent] or usable
        index = self.rng.choice(fresh)
        recent.append(index)
        if len(usable) <= recent.maxlen:  # tiny categories: don't starve
            recent.clear()
            recent.append(index)
        return options[index].format(**values)

    def _refresh(self) -> None:
        if self.path is None:
            return
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            if self._categories:
                log.warning("phrases_missing", path=str(self.path))
            self._mtime, self._categories = None, {}
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            self._categories = validate(json.loads(self.path.read_text(encoding="utf-8")))
            self._recent.clear()
        except (OSError, ValueError) as exc:
            # Keep the last good version if there was one.
            log.warning("phrases_invalid", path=str(self.path), error=str(exc))
