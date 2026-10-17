"""Always-loaded documents: the personality (soul.md) and the user's profile
(profile.md), put at the very start of every LLM system prompt.

The files are re-read whenever their modification time changes, so edits (by
Gustavo or by the bot itself) apply on the next message without a restart.
They come first and don't change between messages, so the provider's prompt
cache covers them (OpenAI caches identical prefixes of 1024+ tokens).
"""

from __future__ import annotations

from pathlib import Path

MAX_CHARS = 8000

PREAMBLE = (
    "## Your personality (soul.md)\n"
    "Follow this personality in the conversation text. Exact formats (JSON, commands, "
    "data, code) stay strict.\n\n"
)


PROFILE_PREAMBLE = (
    "## Who the user is (profile.md)\n"
    "Context about Gustavo, compiled from old conversations: use it to understand requests and "
    "personalize replies. Dates, amounts and plans may be out of date; don't treat them "
    "as current facts and don't repeat personal data without need.\n\n"
)
PROFILE_MAX_CHARS = 30000


class Soul:
    def __init__(
        self, path: Path, *, profile: Path | None = None, max_chars: int = MAX_CHARS
    ) -> None:
        self.path = path
        self.max_chars = max_chars
        self.profile = Soul(profile, max_chars=PROFILE_MAX_CHARS) if profile is not None else None
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
            self._text = self.path.read_text(encoding="utf-8")[: self.max_chars].strip()
            self._mtime = mtime
        return self._text

    def apply(self, system: str | None) -> str | None:
        """Prefix a system prompt with the profile and the personality (stable first)."""
        blocks = []
        if self.profile is not None and (profile := self.profile.text()):
            blocks.append(f"{PROFILE_PREAMBLE}{profile}")
        if soul := self.text():
            blocks.append(f"{PREAMBLE}{soul}")
        if not blocks:
            return system
        return ("\n\n---\n\n".join(blocks) + f"\n\n---\n\n{system or ''}").rstrip()
