"""Entry point: wires settings, logging, services, security and routing into the bot."""

from __future__ import annotations

import asyncio
import io
import sys

from pydantic import ValidationError
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from uva.bootstrap import build_services, setup_storage
from uva.config import Settings, get_settings
from uva.i18n import t
from uva.logging import configure_logging, get_logger
from uva.router import Router
from uva.security import install_guard
from uva.selfmod.reload import listen_for_reloads, reload_plugins
from uva.services import Services
from uva.audio import TELEGRAM_DOWNLOAD_LIMIT
from uva.llm.budget import Scope
from uva.media import to_data_url
from uva.replies import Reply
from uva.telegram_io import bot_commands, keyboard, respond, send_reply, send_text
from uva.tts import SpeechError, should_speak

log = get_logger(__name__)

# Short toast shown the moment a button is pressed (texts in main.button_ack).
BUTTON_ACK = ("confirm", "deny", "shok", "shno", "shal", "wrok", "wrno", "stop")
# Handled at once, even while another message is still being worked on.
STOP_COMMANDS = ("/parar", "/stop")


def is_stop(text: str) -> bool:
    head = text.strip().split(maxsplit=1)[0].lower() if text.strip() else ""
    return head.split("@", 1)[0] in STOP_COMMANDS


def button_ack(verb: str) -> str | None:
    return t(f"main.button_ack.{verb}") if verb in BUTTON_ACK else None


def build_application(settings: Settings, services: Services) -> Application:
    token = settings.telegram_bot_token.get_secret_value()

    async def post_init(app: Application) -> None:
        await setup_storage(services)
        try:
            await app.bot.set_my_commands(bot_commands(services.registry))
        except Exception as exc:  # the menu is a convenience
            log.warning("set_my_commands_failed", error=str(exc))
        # Hot reload of plugins merged by the worker (self-coding).
        if services.busy is not None:
            app.create_task(listen_for_reloads(services, services.busy.redis))
        services.on_code_changed.append(lambda: reload_plugins(services))

    application = Application.builder().token(token).post_init(post_init).build()

    # Must be installed before anything else can see an update.
    install_guard(application, settings.allowed_telegram_user_id)

    router = Router(services)
    # Handlers don't block the update queue (so /parar gets through), but
    # messages are still handled one at a time, in order, through this lock.
    turn_lock = asyncio.Lock()

    def reaction_target(message) -> int | None:
        return message.message_id if settings.telegram_reactions else None

    async def turn(bot, chat_id: int, work, label: str, *, voice_in: bool = False, **kwargs) -> None:
        tracked = services.turns.run(work, label) if services.turns is not None else work
        async with turn_lock:
            replies = await respond(bot, chat_id, services.say, tracked, **kwargs)
        await speak(bot, chat_id, replies, voice_in)

    async def speak(bot, chat_id: int, replies: list[Reply], voice_in: bool) -> None:
        """Optional voice reply (VOICE_REPLIES), within the budget."""
        speaker = services.speaker
        if speaker is None or not should_speak(settings.voice_replies, voice_in):
            return
        text = "\n".join(r.text for r in replies if r.text and not r.failed and r.document is None)
        if not text.strip():
            return
        if services.budget is not None and not (await services.budget.check(Scope.INTERACTIVE)).allowed:
            return
        try:
            audio = await speaker.speak(text)
            await bot.send_voice(chat_id=chat_id, voice=io.BytesIO(audio))
        except (SpeechError, Exception) as exc:
            log.warning("voice_reply_failed", error=str(exc))

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None or message.text is None:
            return
        user_id = update.effective_user.id if update.effective_user else None
        text = message.text
        if is_stop(text):  # right away, without waiting for the turn in progress
            await respond(context.bot, message.chat_id, services.say, router.handle_text(text, user_id=user_id),
                          activity="executando")
            return
        await turn(
            context.bot, message.chat_id, router.handle_text(text, user_id=user_id), text,
            activity="executando" if text.startswith("/") else "pensando", react_to=reaction_target(message),
        )

    async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None or not message.photo:
            return
        user_id = update.effective_user.id if update.effective_user else None

        async def work():
            file = await message.photo[-1].get_file()  # largest size
            photo = to_data_url(bytes(await file.download_as_bytearray()))
            return await router.handle_photo(photo, message.caption or "", user_id=user_id)

        await turn(context.bot, message.chat_id, work(), message.caption or "[photo]", activity="olhando",
                   react_to=reaction_target(message))

    async def on_image_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Images sent "as file" (no Telegram compression)."""
        message = update.effective_message
        if message is None or message.document is None:
            return
        document = message.document
        if (document.file_size or 0) > TELEGRAM_DOWNLOAD_LIMIT:
            text = t("main.file_too_big", phrase=services.say("erro_generico"))
            await send_reply(context.bot, message.chat_id, Reply(text))
            return
        user_id = update.effective_user.id if update.effective_user else None

        async def work():
            file = await document.get_file()
            photo = to_data_url(bytes(await file.download_as_bytearray()), document.mime_type or "image/jpeg")
            return await router.handle_photo(photo, message.caption or "", user_id=user_id)

        await turn(context.bot, message.chat_id, work(), message.caption or "[image]", activity="olhando",
                   react_to=reaction_target(message))

    async def on_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        media = message and (message.voice or message.audio or message.video_note)
        if message is None or media is None:
            return
        if (media.file_size or 0) > TELEGRAM_DOWNLOAD_LIMIT:
            text = t("main.audio_too_big", phrase=services.say("erro_generico"))
            await send_reply(context.bot, message.chat_id, Reply(text))
            return
        user_id = update.effective_user.id if update.effective_user else None

        async def work():
            file = await media.get_file()
            data = bytes(await file.download_as_bytearray())
            return await router.handle_audio(data, seconds=media.duration, user_id=user_id)

        # Download + transcription take a few seconds: say we're on it right away.
        now = services.say("transcrevendo") if services.transcriber is not None else None
        await turn(context.bot, message.chat_id, work(), "[voice]", activity="pensando", now=now,
                   voice_in=True)

    async def on_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None or message.sticker is None:
            return
        sticker = message.sticker
        user_id = update.effective_user.id if update.effective_user else None
        work = router.handle_sticker(
            sticker.file_unique_id, file_id=sticker.file_id, emoji=sticker.emoji or "",
            set_name=sticker.set_name or "", user_id=user_id,
        )
        await turn(context.bot, message.chat_id, work, f"[sticker {sticker.emoji or ''}]",
                   react_to=reaction_target(message))

    async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.data is None or query.message is None:
            return
        # Answer the button at once (Telegram shows a spinner until then).
        verb = query.data.split(":", 1)[0]
        await query.answer(button_ack(verb))
        user_id = update.effective_user.id if update.effective_user else None
        if verb == "pg":
            reply = await router.handle_callback(query.data, user_id=user_id)
            # Page navigation: replace the message in place.
            await send_text(query.edit_message_text, text=reply.text, reply_markup=keyboard(reply))
            return
        # Remove the buttons so the question can't be answered twice.
        await query.edit_message_reply_markup(reply_markup=None)
        work = router.handle_callback(query.data, user_id=user_id)
        if verb == "stop":  # choosing what /parar stops: right away
            await respond(context.bot, query.message.chat.id, services.say, work, activity="executando")
            return
        await turn(
            context.bot, query.message.chat.id, work, f"[button] {verb}",
            activity="executando" if verb in ("shok", "shal", "confirm", "wrok") else "pensando",
        )

    # One text handler (commands included) keeps all routing logic in Router.
    private = filters.ChatType.PRIVATE
    application.add_handler(MessageHandler(filters.TEXT & private, on_text, block=False))
    application.add_handler(MessageHandler(filters.PHOTO & private, on_photo, block=False))
    application.add_handler(MessageHandler(filters.Document.IMAGE & private, on_image_document, block=False))
    audio = filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE
    application.add_handler(MessageHandler(audio & private, on_audio, block=False))
    application.add_handler(MessageHandler(filters.Sticker.ALL & private, on_sticker, block=False))
    application.add_handler(CallbackQueryHandler(on_button, block=False))
    application.add_error_handler(_on_error)
    return application


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("handler_error", exc_info=context.error)


def main() -> None:
    try:
        settings = get_settings()
    except ValidationError as exc:
        # Refuse to start without a token and an allowed user id.
        configure_logging()
        fields = [".".join(map(str, err["loc"])).upper() for err in exc.errors()]
        log.error(
            "invalid_configuration",
            fields=fields,
            hint="Set TELEGRAM_BOT_TOKEN and ALLOWED_TELEGRAM_USER_ID in .env",
        )
        sys.exit(1)

    configure_logging(settings.log_level)
    log.info("starting", allowed_user_id=settings.allowed_telegram_user_id)
    services = build_services(settings)
    build_application(settings, services).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
