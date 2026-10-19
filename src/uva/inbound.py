"""Telegram input before the router: batching, quoted replies and forwards.

* :class:`InboundBuffer` keeps plain messages for a moment of quiet
  (INBOUND_DEBOUNCE_MS) and hands them over as one batch, so a thought typed
  in three messages, a long paste Telegram split in two, or a few forwards
  become one turn. Commands, buttons and /parar flush it first, so the order
  is kept.
* :func:`quoted` and :func:`forward_label` read what Telegram says about a
  message (the one it replies to, where it was forwarded from).
* :func:`compose` turns a batch into what the router needs: the user's own
  text, the context block (quotes and forwards, marked as data) and the label
  kept in the short-term memory and the conversation log.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, tzinfo

from uva.clock import format_local
from uva.safety import as_data, sanitize_untrusted

MAX_MESSAGES = 12
MAX_CHARS = 50_000
MAX_WAIT_SECONDS = 7.5
LONG_MESSAGE_CHARS = 4000  # Telegram splits long pastes around 4096
LONG_WAIT_SECONDS = 1.5
FORWARD_WAIT_SECONDS = 1.0
ALBUM_WAIT_SECONDS = 0.5
QUOTE_CHARS = 1500
LABEL_FORWARD_CHARS = 300


@dataclass(frozen=True, slots=True)
class Quoted:
    """The message a message replies to."""

    from_bot: bool  # one UVA sent
    at: datetime
    text: str
    message_id: int

    def render(self, tz: tzinfo) -> str:
        who = "you (UVA)" if self.from_bot else "the user"
        text = self.text if len(self.text) <= QUOTE_CHARS else self.text[: QUOTE_CHARS - 1] + "…"
        when = format_local(self.at, tz, "%d/%m %H:%M")
        return f"Replying to ({who}, {when}):\n" + as_data("quoted message", text)


@dataclass(slots=True)
class Inbound:
    """One incoming message waiting in the buffer."""

    text: str
    message_id: int | None = None
    forward: str = ""  # "Forwarded from X, 13/10 10:00" (for the LLM, English)
    quoted: Quoted | None = None
    album: str | None = None  # Telegram media_group_id
    received: float = field(default_factory=time.monotonic)
    payload: object = None  # the Telegram message (albums: to download the photo)

    def wait(self, debounce: float) -> float:
        if self.album:
            return max(debounce, ALBUM_WAIT_SECONDS)
        if self.forward:
            return max(debounce, FORWARD_WAIT_SECONDS)
        if len(self.text) >= LONG_MESSAGE_CHARS:
            return max(debounce, LONG_WAIT_SECONDS)
        return debounce


Handler = Callable[[list[Inbound]], Awaitable[None]]


class InboundBuffer:
    """Messages of one chat waiting for a moment of quiet."""

    def __init__(
        self, handler: Handler, *, debounce_ms: int, max_messages: int = MAX_MESSAGES,
        max_chars: int = MAX_CHARS, max_wait: float = MAX_WAIT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.handler = handler
        self.debounce = max(0, debounce_ms) / 1000
        self.max_messages = max_messages
        self.max_chars = max_chars
        self.max_wait = max_wait
        self.clock = clock
        self.items: list[Inbound] = []
        self._timer: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return self.debounce > 0

    def delay(self) -> float:
        """How long to wait from now: the longest wait of what is pending, but
        never past ``max_wait`` after the first message."""
        if not self.items:
            return 0.0
        now = self.clock()
        wait = max(item.wait(self.debounce) for item in self.items)
        left = self.items[0].received + self.max_wait - now
        return max(0.0, min(wait, left))

    def full(self) -> bool:
        return len(self.items) >= self.max_messages or sum(len(i.text) for i in self.items) >= self.max_chars

    async def add(self, item: Inbound) -> None:
        if not self.enabled:
            await self.handler([item])
            return
        item.received = self.clock()
        self.items.append(item)
        if self.full():
            await self.flush()
            return
        self._schedule(self.delay())

    def _schedule(self, delay: float) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = asyncio.get_running_loop().create_task(self._fire(delay))

    async def _fire(self, delay: float) -> None:
        await asyncio.sleep(delay)
        self._timer = None  # flush() must not cancel the task running it
        await self.flush()

    async def flush(self) -> None:
        """Hand over what is pending now (nothing happens when it is empty)."""
        if self._timer is not None and self._timer is not asyncio.current_task():
            self._timer.cancel()
        self._timer = None
        items, self.items = self.items, []
        if items:
            await self.handler(items)


# --- Telegram messages ----------------------------------------------------------


def message_text(message) -> str:
    return (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()


def quoted(message, bot_id: int | None) -> Quoted | None:
    """What ``message`` replies to (None when it is not a reply)."""
    original = getattr(message, "reply_to_message", None)
    if original is None:
        return None
    sender = getattr(original, "from_user", None)
    from_bot = bool(sender is not None and (sender.is_bot or (bot_id is not None and sender.id == bot_id)))
    text = message_text(original) or "[" + _media_kind(original) + "]"
    return Quoted(from_bot, original.date, text, original.message_id)


def _media_kind(message) -> str:
    for kind in ("photo", "document", "voice", "audio", "video", "sticker"):
        if getattr(message, kind, None):
            return kind
    return "message"


def forward_label(message, tz: tzinfo) -> str:
    """'Forwarded from <name/channel>, <date>' ('' when it is not a forward)."""
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return ""
    name = ""
    if (user := getattr(origin, "sender_user", None)) is not None:
        name = " ".join(x for x in (user.first_name, user.last_name) if x) or user.username or ""
    elif getattr(origin, "sender_user_name", None):
        name = origin.sender_user_name
    elif (chat := getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)) is not None:
        name = chat.title or chat.username or ""
        if signature := getattr(origin, "author_signature", None):
            name = f"{name} ({signature})"
    when = format_local(origin.date, tz, "%d/%m/%Y %H:%M") if getattr(origin, "date", None) else "?"
    return f"Forwarded from {name or 'someone'}, {when}"


# --- a batch for the router ----------------------------------------------------------


# The LLM's request when he forwards something without a word of his own.
FORWARD_ONLY_REQUEST = (
    "(The user forwarded this without a comment. Decide what is useful: save it to the inbox, "
    "summarize it, add a task or a reminder; if it is unclear, ask briefly.)"
)


@dataclass(slots=True)
class Composed:
    text: str  # the user's own words ('' when he only forwarded)
    context: str = ""  # for the LLM's context block (English, outside text marked as data)
    label: str = ""  # what the short-term memory and the log keep as his turn
    external: str = ""  # outside content in the batch (forwards), logged as such
    quoted: Quoted | None = None
    message_ids: list[int] = field(default_factory=list)


def compose(items: list[Inbound], tz: tzinfo) -> Composed:
    own = [i.text for i in items if not i.forward and i.text]
    forwards = [i for i in items if i.forward]
    quote = next((i.quoted for i in items if i.quoted is not None), None)
    context = []
    if quote is not None:
        context.append(quote.render(tz))
    for item in forwards:
        clean, _ = sanitize_untrusted(item.text or "(no text)")
        context.append(f"{item.forward}:\n" + as_data("forwarded message", clean))
    text = "\n".join(own)
    labels = [*own] + [
        f"[{item.forward}] {item.text[:LABEL_FORWARD_CHARS]}".rstrip() for item in forwards
    ]
    external = "\n\n".join(f"{item.forward}:\n{item.text}" for item in forwards)
    return Composed(
        text=text, context="\n\n".join(context), label="\n".join(labels), external=external, quoted=quote,
        message_ids=[i.message_id for i in items if i.message_id is not None],
    )
