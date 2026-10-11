"""Entry point: wires settings, logging, security and routing into the bot."""

from __future__ import annotations

import sys

from pydantic import ValidationError
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from uva.commands import build_registry
from uva.config import Settings, get_settings
from uva.logging import configure_logging, get_logger
from uva.router import Router
from uva.security import install_guard

log = get_logger(__name__)


def build_application(settings: Settings) -> Application:
    token = settings.telegram_bot_token.get_secret_value()
    application = Application.builder().token(token).build()

    # Must be installed before anything else can see an update.
    install_guard(application, settings.allowed_telegram_user_id)

    router = Router(build_registry())

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None or message.text is None:
            return
        user_id = update.effective_user.id if update.effective_user else None
        reply = await router.handle_text(message.text, user_id=user_id)
        await message.reply_text(reply)

    # One text handler (commands included) keeps all routing logic in Router.
    application.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, on_text))
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
    build_application(settings).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
