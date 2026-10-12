"""Sending :class:`Reply` objects through the Telegram Bot API."""

from __future__ import annotations

import io

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from uva.replies import Reply


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
        await bot.send_message(chat_id=chat_id, text=reply.text, reply_markup=markup)
