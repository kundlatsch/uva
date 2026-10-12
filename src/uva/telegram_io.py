"""Sending :class:`Reply` objects through the Telegram Bot API."""

from __future__ import annotations

import asyncio
import io

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest

from uva.progress import LiveSink, with_feedback
from uva.replies import Reply
from uva.tgformat import to_html, to_plain

# Telegram's limit applies to the text after the HTML is parsed; the tags we add
# are short, so a raw text within the limit is safe.


def keyboard(reply: Reply) -> InlineKeyboardMarkup | None:
    if not reply.buttons:
        return None
    row = [InlineKeyboardButton(b.text, callback_data=b.data) for b in reply.buttons]
    return InlineKeyboardMarkup([row])


async def send_reply(bot: Bot, chat_id: int, reply: Reply) -> None:
    """Send a reply; multi-part replies become several messages, in order."""
    for message in reply.messages():
        await _send_one(bot, chat_id, message)


async def _send_one(bot: Bot, chat_id: int, reply: Reply) -> None:
    markup = keyboard(reply)
    if reply.document is not None:
        await bot.send_document(
            chat_id=chat_id,
            document=io.BytesIO(reply.document),
            filename=reply.filename,
            caption=reply.text[:1024],  # caption limit
            reply_markup=markup,
        )
    else:
        await send_text(bot.send_message, text=reply.text, reply_markup=markup, chat_id=chat_id)


async def send_text(send, *, text: str, **kwargs):
    """Send/edit with Markdown rendered as Telegram HTML; plain text if rejected."""
    try:
        return await send(text=to_html(text), parse_mode=ParseMode.HTML, **kwargs)
    except BadRequest as exc:
        if "parse" not in str(exc).lower() and "entit" not in str(exc).lower():
            raise
        return await send(text=to_plain(text), **kwargs)


class TelegramSink(LiveSink):
    """Placeholders as real Telegram messages, edited in place."""

    def __init__(self, bot: Bot, chat_id: int, say, activity: str = "pensando") -> None:
        super().__init__(say, activity)
        self.bot = bot
        self.chat_id = chat_id

    async def _post(self, text: str):
        message = await send_text(self.bot.send_message, text=text, chat_id=self.chat_id)
        return message.message_id

    async def _edit(self, handle, text: str, buttons=None) -> None:
        markup = keyboard(Reply(text, buttons=list(buttons or [])))
        try:
            await send_text(
                self.bot.edit_message_text, text=text, chat_id=self.chat_id, message_id=handle,
                reply_markup=markup,
            )
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise

    async def _post_reply(self, reply: Reply) -> None:
        await _send_one(self.bot, self.chat_id, reply)


async def _keep_typing(bot: Bot, chat_id: int, stop: asyncio.Event) -> None:
    """"digitando…" right away, renewed until the reply is out (it lasts ~5 s)."""
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=4)
        except TimeoutError:
            pass


async def respond(bot: Bot, chat_id: int, say, work, *, activity: str = "pensando", now: str | None = None) -> None:
    """Handle one incoming update with instant feedback.

    "digitando…" goes out at once; if the reply takes longer than a moment, a
    phrase for the activity shows up and is then edited into the reply.
    ``now`` shows that phrase immediately (e.g. before downloading an audio).
    """
    stop = asyncio.Event()
    typing = asyncio.create_task(_keep_typing(bot, chat_id, stop))
    sink = TelegramSink(bot, chat_id, say, activity)
    try:
        if now:
            await sink.send(now)
        result = await with_feedback(sink, work)
    finally:
        stop.set()
    replies = result if isinstance(result, list) else [result]
    await sink.deliver([m for r in replies for m in r.messages()])
    await typing
