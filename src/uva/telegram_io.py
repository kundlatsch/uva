"""Sending :class:`Reply` objects through the Telegram Bot API."""

from __future__ import annotations

import io

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest

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
