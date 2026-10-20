"""Early feedback while a message is being handled.

The transport (Telegram, the dashboard) sets ``progress_sink`` for one
message. By default nothing is posted while the work runs: Telegram shows
"typing…" and a reaction on the incoming message. Then:

* work that knows it takes long (a command or action with ``placeholder``
  set, or code calling :func:`slow`) opts in to a placeholder message: a
  ready-made phrase for the current activity ("🤔 Pensando…", "⚙️ Rodando…")
  shown right away, which the final reply replaces (Telegram edits that same
  message);
* code that knows what it is doing calls :func:`activity` ("executando",
  "procurando"...): the phrase follows it, and an already visible placeholder
  is updated;
* :func:`placeholder` with ``keep=True`` sends a real message right away,
  e.g. the LLM's own reply before its slow actions run.

Without a sink (tests, background tasks) these are no-ops.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Protocol


class ProgressSink(Protocol):
    async def send(self, text: str, keep: bool = False) -> None: ...

    async def set_activity(self, category: str) -> None: ...

    async def show_placeholder(self) -> None: ...


progress_sink: ContextVar[ProgressSink | None] = ContextVar("uva_progress_sink", default=None)


async def placeholder(text: str, *, keep: bool = False) -> bool:
    """Show ``text`` now if a transport is listening; returns whether it was shown."""
    sink = progress_sink.get()
    if sink is None or not text.strip():
        return False
    try:
        await sink.send(text, keep)
    except Exception:
        return False  # feedback is best effort; the real reply still comes
    return True


async def activity(category: str) -> None:
    """What the bot is doing now (a phrase category of data/fallback_phrases.json)."""
    sink = progress_sink.get()
    if sink is None:
        return
    try:
        await sink.set_activity(category)
    except Exception:
        pass


async def slow(category: str | None = None) -> None:
    """This work takes a while: show a placeholder now (for ``category`` if given).

    Opt-in: without it the user only sees the reaction and "typing…".
    """
    sink = progress_sink.get()
    if sink is None:
        return
    try:
        if category:
            await sink.set_activity(category)
        await sink.show_placeholder()
    except Exception:
        pass


class LiveSink:
    """Placeholder logic shared by the transports; they implement _post/_edit.

    One replaceable message at a time: a placeholder is posted once, edited as
    the activity changes, and finally edited into the first message of the reply.
    """

    def __init__(self, say, activity: str = "pensando") -> None:
        self.say = say
        self.activity = activity
        self.handle = None  # the replaceable message
        self.posted = False  # anything shown at all
        self.wanted = False  # the work opted in to a placeholder (slow())
        import asyncio

        self._lock = asyncio.Lock()

    async def _post(self, text: str):  # -> handle
        raise NotImplementedError

    async def _edit(self, handle, text: str, buttons=None) -> None:
        raise NotImplementedError

    async def _post_reply(self, reply) -> None:
        raise NotImplementedError

    async def send(self, text: str, keep: bool = False) -> None:
        async with self._lock:
            await self._send(text, keep)

    async def _send(self, text: str, keep: bool) -> None:
        self.posted = True
        if not keep:
            if self.handle is None:
                self.handle = await self._post(text)
            else:
                await self._edit(self.handle, text)
            return
        if self.handle is not None:
            # The placeholder becomes this real message; the rest gets a new one.
            await self._edit(self.handle, text)
            self.handle = None
        else:
            await self._post(text)
        if self.wanted:
            self.handle = await self._post(self.say(self.activity))

    async def set_activity(self, category: str) -> None:
        async with self._lock:
            if category == self.activity:
                return
            self.activity = category
            if self.handle is not None:
                await self._edit(self.handle, self.say(category))

    async def show_placeholder(self) -> None:
        async with self._lock:
            self.wanted = True
            if self.handle is None:
                await self._send(self.say(self.activity), False)

    async def deliver(self, messages: list) -> None:
        """Send the final reply's messages; the first one replaces the placeholder."""
        async with self._lock:
            messages = list(messages)
            if self.handle is not None:
                if messages and messages[0].document is None:
                    first = messages.pop(0)
                    await self._edit(self.handle, first.text, first.buttons)
                else:
                    await self._edit(self.handle, "👇")
                self.handle = None
            for message in messages:
                await self._post_reply(message)


async def with_feedback(sink: LiveSink, work):
    """Run the ``work`` coroutine with ``sink`` listening for progress."""
    import asyncio

    token = progress_sink.set(sink)
    try:
        task = asyncio.ensure_future(work)  # copies the context: the sink is visible inside
    finally:
        progress_sink.reset(token)
    return await task
