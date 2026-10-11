"""Single-user safeguard applied before every other handler."""

from __future__ import annotations

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import Application, ApplicationHandlerStop, ContextTypes, TypeHandler

from uva.logging import get_logger

log = get_logger(__name__)

# Handlers in lower groups run first; -100 keeps the guard ahead of anything
# registered later.
SECURITY_GROUP = -100


def is_authorized(update: object, allowed_user_id: int) -> bool:
    """True only for updates from the owner in a private chat."""
    if not isinstance(update, Update):
        return False
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return False
    return user.id == allowed_user_id and chat.type == ChatType.PRIVATE


def make_guard(allowed_user_id: int):
    """Build the guard callback bound to the allowed user id."""

    async def guard(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        if is_authorized(update, allowed_user_id):
            return
        user = update.effective_user if isinstance(update, Update) else None
        chat = update.effective_chat if isinstance(update, Update) else None
        log.warning(
            "unauthorized_update_ignored",
            user_id=user.id if user else None,
            username=user.username if user else None,
            chat_id=chat.id if chat else None,
            chat_type=chat.type if chat else None,
        )
        # Stop processing silently: no reply is sent to strangers.
        raise ApplicationHandlerStop

    return guard


def install_guard(application: Application, allowed_user_id: int) -> None:
    application.add_handler(
        TypeHandler(Update, make_guard(allowed_user_id)), group=SECURITY_GROUP
    )
