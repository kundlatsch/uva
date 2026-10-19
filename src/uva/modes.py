"""/modo: a temporary personality overlay for the current session.

A mode changes the tone and format of the replies (not the rules) until the
session ends or ``/modo normal``. It lives in the short-term memory session,
so nothing is edited or committed. Built-in modes are below (English: only
the LLM reads them); custom ones come from ``data/modes.json``
(``{"name": "instruction", ...}``).
"""

from __future__ import annotations

import json
from pathlib import Path

from uva.logging import get_logger
from uva.text import normalize

log = get_logger(__name__)

BUILTIN = {
    "serio": "Be serious and professional: no jokes, no swearing, no irony. Keep the facts precise.",
    "conciso": "Be as brief as possible: one or two sentences, bullets only when they save words.",
    "professor": (
        "Teach: explain the why step by step, with a simple example, check understanding with one "
        "short question at the end."
    ),
}
NORMAL = ("normal", "padrao", "default", "off", "nenhum")


def available(path: Path) -> dict[str, str]:
    modes = dict(BUILTIN)
    try:
        custom = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return modes
    except (OSError, ValueError) as exc:
        log.warning("modes_file_invalid", path=str(path), error=str(exc))
        return modes
    if isinstance(custom, dict):
        modes.update({normalize(str(k)).replace(" ", "_"): str(v) for k, v in custom.items() if str(v).strip()})
    return modes


def find(name: str, path: Path) -> tuple[str, str] | None:
    key = normalize(name).replace(" ", "_")
    modes = available(path)
    return (key, modes[key]) if key in modes else None


def overlay(name: str, instruction: str) -> str:
    """The block for the message context."""
    return (
        f"Mode for this session ({name}), set by the user: {instruction} "
        "It changes the tone and format of your replies, not the rules or the JSON format."
    )
