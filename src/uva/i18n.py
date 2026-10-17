"""Translations of everything the user reads.

Strings live in ``src/uva/locales/<language>/<namespace>.json``; the file name
is the namespace, so ``t("tasks.none")`` reads the key ``"none"`` from
``locales/<language>/tasks.json``. Nested objects become dotted keys.

The language comes from ``BOT_LANGUAGE`` in .env (``en`` by default, or
``pt``) and is set once at startup with :func:`set_language`. A key missing in
the current language falls back to English, and a key missing everywhere is
returned as is (and logged), so a gap never breaks a reply.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from uva.logging import get_logger

log = get_logger(__name__)

LOCALES_DIR = Path(__file__).parent / "locales"
SUPPORTED = ("en", "pt")
DEFAULT = "en"
# Shown to the LLM ("write to the user in ...").
NAMES = {"en": "English", "pt": "Brazilian Portuguese"}

_language = DEFAULT


def normalize(language: str) -> str:
    """'pt-BR', 'PT_br', 'pt' -> 'pt'. Raises ValueError for unsupported ones."""
    code = language.strip().lower().replace("_", "-").split("-", 1)[0]
    if code not in SUPPORTED:
        raise ValueError(f"unsupported language {language!r}; use one of {', '.join(SUPPORTED)}")
    return code


def set_language(language: str) -> None:
    global _language
    _language = normalize(language)


def get_language() -> str:
    return _language


def language_name(language: str | None = None) -> str:
    return NAMES[language or _language]


def _flatten(prefix: str, node: Any, out: dict[str, Any]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            _flatten(f"{prefix}.{key}" if prefix else key, value, out)
    else:
        out[prefix] = node


@cache
def catalog(language: str) -> dict[str, Any]:
    """Every key of a language: {"tasks.none": "...", ...}."""
    out: dict[str, Any] = {}
    for path in sorted((LOCALES_DIR / language).glob("*.json")):
        _flatten(path.stem, json.loads(path.read_text(encoding="utf-8")), out)
    return out


def _lookup(key: str, language: str | None) -> Any:
    for lang in (language or _language, DEFAULT):
        value = catalog(lang).get(key)
        if value is not None:
            return value
    log.warning("i18n_missing_key", key=key, language=language or _language)
    return None


def t(key: str, /, *, lang: str | None = None, **values: Any) -> str:
    """The text of ``key`` in the current language, filled with ``values``."""
    text = _lookup(key, lang)
    if text is None:
        return key
    if isinstance(text, list):
        text = "\n".join(text)
    return text.format(**values) if values else text


def t_list(key: str, /, *, lang: str | None = None) -> list[str]:
    """A list value (e.g. weekday names)."""
    value = _lookup(key, lang)
    return list(value) if isinstance(value, list) else []
