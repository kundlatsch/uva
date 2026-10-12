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
from pathlib import Path
from typing import Any

from uva.logging import get_logger

log = get_logger(__name__)

MAX_PHRASE_CHARS = 300
RECENT_PER_CATEGORY = 3

# Neutral phrases used when the file is unavailable or lacks a category.
BUILTIN: dict[str, str] = {
    "sem_credito": "A LLM está sem orçamento agora; os comandos continuam funcionando.",
    "llm_erro": "Não consegui falar com a LLM agora. Tente de novo em instantes.",
    "nao_entendi": "Não entendi. Envie /help para ver o que eu sei fazer.",
    "ocupado": "Já estou fazendo outra coisa; tente de novo quando terminar.",
    "tarefa_falhou": "Uma tarefa falhou.",
    "tarefa_ok": "Tarefa concluída.",
    "pesquisando": "🔎 Pesquisando…",
    "saudacao": "Olá! Em que posso ajudar?",
    "agradecimento": "De nada!",
    "despedida": "Até mais!",
    "confirmacao": "Feito.",
    "cancelado": "Ok, cancelado.",
    "sem_resultado": "Não encontrei nada.",
    "erro_generico": "Algo deu errado do meu lado. O erro foi registrado.",
    "transcrevendo": "🎙️ Transcrevendo o áudio…",
}


class PhrasesError(ValueError):
    pass


def placeholders(phrase: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(phrase) if name}


def validate(data: Any) -> dict[str, list[str]]:
    """Check the file structure; returns the categories."""
    if not isinstance(data, dict) or not isinstance(data.get("version"), int):
        raise PhrasesError('precisa ser um objeto com "version" (número)')
    documented = data.get("placeholders", {})
    if not isinstance(documented, dict):
        raise PhrasesError('"placeholders" precisa ser um objeto')
    categories = data.get("categories")
    if not isinstance(categories, dict) or not categories:
        raise PhrasesError('"categories" precisa ser um objeto não vazio')
    for name, phrases in categories.items():
        if not isinstance(phrases, list) or not phrases:
            raise PhrasesError(f"categoria {name!r} precisa ser uma lista não vazia")
        for phrase in phrases:
            if not isinstance(phrase, str) or not phrase.strip():
                raise PhrasesError(f"frase vazia ou inválida em {name!r}")
            if len(phrase) > MAX_PHRASE_CHARS:
                raise PhrasesError(f"frase longa demais em {name!r}: {phrase[:40]}…")
            try:
                unknown = placeholders(phrase) - set(documented)
            except ValueError as exc:
                raise PhrasesError(f"chaves mal formadas em {name!r}: {phrase}") from exc
            if unknown:
                raise PhrasesError(f"placeholder não documentado {unknown} em {name!r}")
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
            return BUILTIN.get(category, BUILTIN["erro_generico"])

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
