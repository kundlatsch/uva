"""Transport-agnostic replies produced by commands and the router."""

from __future__ import annotations

from dataclasses import dataclass, field

# Telegram rejects messages longer than this.
TELEGRAM_TEXT_LIMIT = 4096
# One page of a paginated reply (room left for the "página X/Y" footer).
PAGE_CHARS = 3000
PAGE_LINES = 40


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
    # Long content split into pages: shown one at a time with ◀ ▶ buttons.
    pages: list[str] = field(default_factory=list)
    # Replace the message the button was pressed on instead of sending a new one.
    edit: bool = False

    @classmethod
    def paginated(cls, text: str, header: str = "") -> Reply:
        """A reply that becomes pages when it doesn't fit in one message."""
        pages = paginate(text, header)
        return cls(text=pages[0] if len(pages) == 1 else text, pages=pages if len(pages) > 1 else [])

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


def paginate(
    text: str, header: str = "", max_chars: int = PAGE_CHARS, max_lines: int = PAGE_LINES
) -> list[str]:
    """Split on line boundaries; each page repeats ``header`` (if any) at the top."""
    budget = max_chars - (len(header) + 2 if header else 0)
    lines: list[str] = []
    for line in text.splitlines():
        # A single line longer than a page is hard-wrapped.
        lines += [line[i : i + budget] for i in range(0, len(line), budget)] or [""]

    pages: list[list[str]] = [[]]
    size = 0
    for line in lines:
        if pages[-1] and (size + len(line) + 1 > budget or len(pages[-1]) >= max_lines):
            pages.append([])
            size = 0
        pages[-1].append(line)
        size += len(line) + 1
    chunks = ["\n".join(page).strip("\n") for page in pages]
    chunks = [c for c in chunks if c.strip()] or [""]
    return [f"{header}\n\n{chunk}" if header else chunk for chunk in chunks]
