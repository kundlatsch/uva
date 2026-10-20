"""Sending :class:`Reply` objects through the Telegram Bot API."""

from __future__ import annotations

import asyncio
import io

import re

from telegram import Bot, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest

from uva.progress import LiveSink, with_feedback
from uva.replies import Reply
from uva.tgformat import to_html, to_plain

# Telegram's limit applies to the text after the HTML is parsed; the tags we add
# are short, so a raw text within the limit is safe.


# Telegram's limits for the command menu (setMyCommands).
MAX_MENU_COMMANDS = 100
_MENU_NAME = re.compile(r"^[a-z0-9_]{1,32}$")


def bot_commands(registry) -> list[BotCommand]:
    """The "/" menu: every command (idle plugins left out) with its description
    in the bot language, up to Telegram's limit of 100."""
    out = []
    for command in registry:
        if command.name in registry.idle or not _MENU_NAME.match(command.name):
            continue
        description = " ".join(command.description.split())[:256] or command.name
        out.append(BotCommand(command.name, description))
    return out[:MAX_MENU_COMMANDS]


def keyboard(reply: Reply) -> InlineKeyboardMarkup | None:
    if not reply.buttons:
        return None
    row = [InlineKeyboardButton(b.text, callback_data=b.data) for b in reply.buttons]
    return InlineKeyboardMarkup([row])


async def send_reply(bot: Bot, chat_id: int, reply: Reply) -> list[int]:
    """Send a reply; multi-part replies become several messages, in order.
    Returns the Telegram message ids."""
    ids = []
    for message in reply.messages():
        sent = await _send_one(bot, chat_id, message)
        if isinstance(getattr(sent, "message_id", None), int):
            ids.append(sent.message_id)
    return ids


async def _send_one(bot: Bot, chat_id: int, reply: Reply):
    markup = keyboard(reply)
    if reply.image is not None and reply.document is None:
        return await bot.send_photo(
            chat_id=chat_id, photo=io.BytesIO(reply.image), caption=reply.text[:1024] or None, reply_markup=markup
        )
    elif reply.document is not None:
        return await bot.send_document(
            chat_id=chat_id,
            document=io.BytesIO(reply.document),
            filename=reply.filename,
            caption=reply.text[:1024],  # caption limit
            reply_markup=markup,
        )
    return await send_text(bot.send_message, text=reply.text, reply_markup=markup, chat_id=chat_id)


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


RECEIVED, FAILED = "👀", "👎"


async def react(bot: Bot, chat_id: int, message_id: int | None, emoji: str | None) -> None:
    """Set (or clear, with None) the bot's reaction on a message. Best effort."""
    if message_id is None:
        return
    try:
        await bot.set_message_reaction(chat_id=chat_id, message_id=message_id, reaction=emoji)
    except Exception:
        pass  # old clients, deleted messages...


async def respond(
    bot: Bot, chat_id: int, say, work, *, activity: str = "pensando", now: str | None = None,
    react_to: int | None = None,
) -> list[Reply]:
    """Handle one incoming update with instant feedback; returns the replies sent.

    "digitando…" goes out at once and, with ``react_to``, the incoming message
    gets 👀; that is all the feedback by default. When the reply is out the
    reaction is removed (👎 if something failed). Slow work can opt in to a
    placeholder phrase (:func:`uva.progress.slow`), edited into the reply;
    ``now`` shows one immediately.
    """
    await react(bot, chat_id, react_to, RECEIVED)
    stop = asyncio.Event()
    typing = asyncio.create_task(_keep_typing(bot, chat_id, stop))
    sink = TelegramSink(bot, chat_id, say, activity)
    try:
        if now:
            await sink.send(now)
        result = await with_feedback(sink, work)
    except Exception:
        stop.set()
        await react(bot, chat_id, react_to, FAILED)
        raise
    finally:
        stop.set()
    replies = result if isinstance(result, list) else [result]
    await sink.deliver([m for r in replies for m in r.messages()])
    await typing
    if react_to is not None:
        failed = any(r.failed for r in replies)
        await react(bot, chat_id, react_to, FAILED if failed else None)
    return replies
