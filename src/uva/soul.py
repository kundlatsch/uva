"""The bot's personality (soul.md), injected into every LLM system prompt.

The file is re-read whenever its modification time changes, so edits (by
Gustavo or by the bot itself) apply on the next message without a restart.
"""

from __future__ import annotations

from pathlib import Path

MAX_CHARS = 8000

PREAMBLE = (
    "## Sua personalidade (soul.md)\n"
    "Siga esta personalidade no texto da conversa. Formatos exatos (JSON, comandos, "
    "dados, código) continuam estritos.\n\n"
)


class Soul:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._mtime: float | None = None
        self._text = ""

    def text(self) -> str:
        """Current contents ('' if the file is missing)."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime, self._text = None, ""
            return ""
        if mtime != self._mtime:
            self._text = self.path.read_text(encoding="utf-8")[:MAX_CHARS].strip()
            self._mtime = mtime
        return self._text

    def apply(self, system: str | None) -> str | None:
        """Prefix a system prompt with the personality."""
        soul = self.text()
        if not soul:
            return system
        return f"{PREAMBLE}{soul}\n\n---\n\n{system or ''}".rstrip()
