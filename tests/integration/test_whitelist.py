"""The whitelist through the real dispatcher, for every kind of update Telegram can deliver.

The unit tests of ``WhitelistMiddleware`` inject ``event_from_user`` by hand; here aiogram
resolves the sender itself, so a mismatch between our assumption and aiogram's user
resolution (callback queries, channel posts, anonymous senders) would show up.
"""

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineQuery,
    Message,
    TelegramObject,
    Update,
    User,
)

from app.bot.dispatcher import build_dispatcher
from app.services.fake import create_fake_services
from tests.bot.fakes import ADMIN_ID, STRANGER_ID, RecordingSession, text_update

WHITELIST = frozenset({ADMIN_ID})
# Telegram's sender for messages posted anonymously on behalf of a group.
GROUP_ANONYMOUS_BOT_ID = 1087968824
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def user(user_id: int) -> User:
    return User(id=user_id, is_bot=False, first_name="Test")


def callback_update(user_id: int) -> Update:
    """A button press under a message the bot sent in the user's private chat."""
    message = Message(message_id=5, date=NOW, chat=Chat(id=user_id, type="private"), text="Menyu")
    query = CallbackQuery(
        id="cb", from_user=user(user_id), chat_instance="ci", data="menu", message=message
    )
    return Update(update_id=10, callback_query=query)


def inline_mode_callback_update(user_id: int) -> Update:
    """A button press under an inline-mode message: Telegram sends no chat at all."""
    query = CallbackQuery(
        id="cb2", from_user=user(user_id), chat_instance="ci", data="menu", inline_message_id="im"
    )
    return Update(update_id=16, callback_query=query)


def edited_message_update(user_id: int) -> Update:
    message = Message(
        message_id=1,
        date=NOW,
        edit_date=1,
        chat=Chat(id=user_id, type="private"),
        from_user=user(user_id),
        text="/start",
    )
    return Update(update_id=11, edited_message=message)


def inline_query_update(user_id: int) -> Update:
    query = InlineQuery(id="iq", from_user=user(user_id), query="Arsenal", offset="")
    return Update(update_id=12, inline_query=query)


def channel_post_update() -> Update:
    channel = Chat(id=-100123, type="channel", title="Kanal")
    post = Message(message_id=1, date=NOW, chat=channel, sender_chat=channel, text="/start")
    return Update(update_id=13, channel_post=post)


def anonymous_group_admin_update() -> Update:
    group = Chat(id=-100456, type="supergroup", title="Guruh")
    message = Message(
        message_id=1,
        date=NOW,
        chat=group,
        sender_chat=group,
        from_user=User(id=GROUP_ANONYMOUS_BOT_ID, is_bot=True, first_name="Group"),
        text="/start",
    )
    return Update(update_id=14, message=message)


def edited_channel_post_update() -> Update:
    channel = Chat(id=-100123, type="channel", title="Kanal")
    post = Message(
        message_id=1, date=NOW, edit_date=1, chat=channel, sender_chat=channel, text="/start"
    )
    return Update(update_id=15, edited_channel_post=post)


@pytest.fixture
def seen() -> list[TelegramObject]:
    return []


@pytest.fixture
def dispatcher(seen: list[TelegramObject]) -> Dispatcher:
    """The production dispatcher plus a probe router that records whatever reaches it."""
    dispatcher = build_dispatcher(WHITELIST, create_fake_services())
    probe = Router(name="probe")

    async def record(event: TelegramObject) -> None:
        seen.append(event)

    for observer in (
        probe.message,
        probe.edited_message,
        probe.callback_query,
        probe.inline_query,
        probe.channel_post,
        probe.edited_channel_post,
    ):
        observer.register(record)
    dispatcher.include_router(probe)
    # Put the probe first: the bot's own catch-all router would otherwise consume updates.
    dispatcher.sub_routers.insert(0, dispatcher.sub_routers.pop())
    return dispatcher


@pytest.mark.parametrize(
    "make_update",
    [
        lambda: text_update(STRANGER_ID, "/start"),
        lambda: callback_update(STRANGER_ID),
        lambda: edited_message_update(STRANGER_ID),
        lambda: inline_query_update(STRANGER_ID),
        channel_post_update,
        anonymous_group_admin_update,
        edited_channel_post_update,
        lambda: inline_query_update(ADMIN_ID),
        lambda: inline_mode_callback_update(ADMIN_ID),
    ],
    ids=[
        "stranger-message",
        "stranger-callback",
        "stranger-edited",
        "stranger-inline",
        "channel-post-no-user",
        "anonymous-group-admin",
        "edited-channel-post-no-user",
        "admin-inline-query-no-chat",
        "admin-inline-mode-callback-no-chat",
    ],
)
async def test_updates_not_from_a_whitelisted_private_chat_reach_no_handler(
    dispatcher: Dispatcher,
    bot: Bot,
    session: RecordingSession,
    seen: list[TelegramObject],
    make_update: Callable[[], Update],
) -> None:
    await dispatcher.feed_update(bot, make_update())

    assert seen == []
    assert session.requests == []


@pytest.mark.parametrize(
    "make_update",
    [
        lambda: callback_update(ADMIN_ID),
        lambda: edited_message_update(ADMIN_ID),
        lambda: text_update(ADMIN_ID, "salom"),
    ],
    ids=["admin-callback", "admin-edited", "admin-message"],
)
async def test_updates_from_a_whitelisted_user_reach_handlers(
    dispatcher: Dispatcher,
    bot: Bot,
    seen: list[TelegramObject],
    make_update: Callable[[], Update],
) -> None:
    await dispatcher.feed_update(bot, make_update())

    assert len(seen) == 1


@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
async def test_admin_start_in_a_group_is_ignored(
    bot: Bot, session: RecordingSession, chat_type: str
) -> None:
    # Personal bot: signals and stakes must never be posted where others can read them.
    group = Chat(id=-100789, type=chat_type, title="Guruh")
    message = Message(message_id=1, date=NOW, chat=group, from_user=user(ADMIN_ID), text="/start")

    await build_dispatcher(WHITELIST, create_fake_services()).feed_update(
        bot, Update(update_id=20, message=message)
    )

    assert session.requests == []


async def test_stranger_in_a_group_with_the_admin_is_still_ignored(
    bot: Bot, session: RecordingSession
) -> None:
    group = Chat(id=-100789, type="group", title="Guruh")
    message = Message(
        message_id=2, date=NOW, chat=group, from_user=user(STRANGER_ID), text="/start"
    )

    await build_dispatcher(WHITELIST, create_fake_services()).feed_update(
        bot, Update(update_id=21, message=message)
    )

    assert session.requests == []
