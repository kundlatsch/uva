"""Transport-agnostic replies produced by commands and the router."""

from __future__ import annotations

from dataclasses import dataclass, field

# Telegram rejects messages longer than this.
TELEGRAM_TEXT_LIMIT = 4096


@dataclass(frozen=True, slots=True)
class Button:
    text: str
    data: str  # callback data, max 64 bytes on Telegram


@dataclass(slots=True)
class Reply:
    text: str
    buttons: list[Button] = field(default_factory=list)
    # Optional file attachment (e.g. a long /log). `text` becomes the caption.
    document: bytes | None = None
    filename: str = "uva.md"

    @classmethod
    def coerce(cls, value: str | Reply) -> Reply:
        return value if isinstance(value, Reply) else cls(text=value)

    def fit_for_telegram(self) -> Reply:
        """Move text that is too long for a message into a file attachment."""
        if self.document is not None or len(self.text) <= TELEGRAM_TEXT_LIMIT:
            return self
        return Reply(
            text="A resposta ficou longa, segue em arquivo.",
            buttons=self.buttons,
            document=self.text.encode("utf-8"),
            filename=self.filename,
        )
