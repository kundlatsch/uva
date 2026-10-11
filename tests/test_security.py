from datetime import UTC, datetime

import pytest
from telegram import Chat, Message, Update, User
from telegram.ext import ApplicationHandlerStop

from uva.security import is_authorized, make_guard

OWNER_ID = 1001
STRANGER_ID = 2002


def make_update(user_id: int | None, chat_type: str = Chat.PRIVATE) -> Update:
    user = User(id=user_id, first_name="Test", is_bot=False) if user_id is not None else None
    chat = Chat(id=user_id or 1, type=chat_type)
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=chat,
        from_user=user,
        text="/ping",
    )
    return Update(update_id=1, message=message)


def test_owner_in_private_chat_is_authorized():
    assert is_authorized(make_update(OWNER_ID), OWNER_ID)


def test_other_user_is_rejected():
    assert not is_authorized(make_update(STRANGER_ID), OWNER_ID)


@pytest.mark.parametrize("chat_type", [Chat.GROUP, Chat.SUPERGROUP, Chat.CHANNEL])
def test_owner_outside_private_chat_is_rejected(chat_type):
    assert not is_authorized(make_update(OWNER_ID, chat_type), OWNER_ID)


def test_update_without_user_is_rejected():
    assert not is_authorized(make_update(None), OWNER_ID)


def test_non_update_object_is_rejected():
    assert not is_authorized(object(), OWNER_ID)


async def test_guard_lets_owner_through():
    guard = make_guard(OWNER_ID)
    await guard(make_update(OWNER_ID), None)  # no exception


async def test_guard_stops_strangers():
    guard = make_guard(OWNER_ID)
    with pytest.raises(ApplicationHandlerStop):
        await guard(make_update(STRANGER_ID), None)
