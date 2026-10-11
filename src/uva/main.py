"""Entry point: wires settings, logging, services, security and routing into the bot."""

from __future__ import annotations

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
from uva.logging import configure_logging, get_logger
from uva.router import Router
from uva.security import install_guard
from uva.selfmod.reload import listen_for_reloads, reload_plugins
from uva.services import Services
from uva.telegram_io import send_reply

log = get_logger(__name__)


def build_application(settings: Settings, services: Services) -> Application:
    token = settings.telegram_bot_token.get_secret_value()

    async def post_init(app: Application) -> None:
        await setup_storage(services)
        # Hot reload of plugins merged by the worker (self-coding).
        if services.busy is not None:
            app.create_task(listen_for_reloads(services, services.busy.redis))
        services.on_code_changed.append(lambda: reload_plugins(services))

    application = Application.builder().token(token).post_init(post_init).build()

    # Must be installed before anything else can see an update.
    install_guard(application, settings.allowed_telegram_user_id)

    router = Router(services)

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None or message.text is None:
            return
        user_id = update.effective_user.id if update.effective_user else None
        reply = await router.handle_text(message.text, user_id=user_id)
        await send_reply(context.bot, message.chat_id, reply)

    async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.data is None or query.message is None:
            return
        await query.answer()
        # Remove the buttons so the question can't be answered twice.
        await query.edit_message_reply_markup(reply_markup=None)
        user_id = update.effective_user.id if update.effective_user else None
        reply = await router.handle_callback(query.data, user_id=user_id)
        await send_reply(context.bot, query.message.chat.id, reply)

    # One text handler (commands included) keeps all routing logic in Router.
    application.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, on_text))
    application.add_handler(CallbackQueryHandler(on_button))
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
            hint="Preencha TELEGRAM_BOT_TOKEN e ALLOWED_TELEGRAM_USER_ID no .env",
        )
        sys.exit(1)

    configure_logging(settings.log_level)
    log.info("starting", allowed_user_id=settings.allowed_telegram_user_id)
    services = build_services(settings)
    build_application(settings, services).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
