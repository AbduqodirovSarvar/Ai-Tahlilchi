"""Tests for app.bot.startup_notice."""

from unittest.mock import AsyncMock

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage

from app.bot.startup_notice import announce_startup

TOKEN = "42:TEST-TOKEN-VALUE"


def forbidden() -> TelegramForbiddenError:
    return TelegramForbiddenError(
        method=SendMessage(chat_id=1, text="x"), message="Forbidden: bot was blocked"
    )


async def test_every_admin_gets_the_build_and_backend() -> None:
    bot = Bot(TOKEN)
    bot.send_message = AsyncMock()  # type: ignore[method-assign]  # no network in tests

    told = await announce_startup(bot, {20, 10}, build="1.0.0+abc", backend="fake")

    assert told == 2
    chats = [call.args[0] for call in bot.send_message.await_args_list]
    assert chats == [10, 20]
    text = bot.send_message.await_args_list[0].args[1]
    assert "1.0.0+abc" in text
    assert "fake" in text


async def test_an_unreachable_admin_does_not_stop_the_others() -> None:
    bot = Bot(TOKEN)
    bot.send_message = AsyncMock(side_effect=[forbidden(), None])  # type: ignore[method-assign]

    told = await announce_startup(bot, {1, 2}, build="b", backend="real")

    assert told == 1
    assert bot.send_message.await_count == 2
