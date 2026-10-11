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
    # Short version kept in the short-term memory (command outputs are summarized).
    summary: str | None = None
    # When set, each part is sent as its own message (``text`` is all of them
    # joined, for logs and memory). Used for the LLM reply + one per action.
    parts: list[str] = field(default_factory=list)

    @classmethod
    def from_parts(cls, parts: list[str]) -> Reply:
        return cls(text="\n\n".join(parts), parts=list(parts))

    def messages(self) -> list[Reply]:
        """The Telegram messages to send, in order (buttons go on the last one)."""
        if len(self.parts) <= 1:
            return [self.fit_for_telegram()]
        out = [Reply(text=part, filename=self.filename).fit_for_telegram() for part in self.parts]
        out[-1].buttons = self.buttons
        return out

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
            summary=self.summary,
        )
