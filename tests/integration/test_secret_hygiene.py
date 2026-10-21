"""The Telegram bot token must never reach the logs, even when a request fails.

aiogram builds request URLs as ``https://api.telegram.org/bot<TOKEN>/<method>``. When a
handler's reply fails (network blip, Telegram outage) the exception travels to our global
error handler, which logs it. These tests drive that real path offline: aiohttp's request
is replaced by one that raises a connection error.
"""

from collections.abc import AsyncIterator
from typing import Literal, NoReturn

import aiohttp
import pytest
from aiogram import Bot

from app.bot.dispatcher import build_dispatcher
from app.logging import configure_logging
from tests.bot.fakes import ADMIN_ID, text_update

TOKEN = "42:SUPERSECRET-TOKEN"


@pytest.fixture
async def offline_bot(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Bot]:
    """A bot with aiogram's real HTTP session whose every request fails before the network."""

    async def refuse(*_args: object, **_kwargs: object) -> NoReturn:
        msg = "network is down"
        raise aiohttp.ClientConnectionError(msg)

    monkeypatch.setattr(aiohttp.ClientSession, "_request", refuse)
    bot = Bot(token=TOKEN)
    yield bot
    await bot.session.close()


async def fail_to_reply(bot: Bot, app_env: Literal["dev", "prod"]) -> None:
    configure_logging(level="INFO", app_env=app_env)
    dispatcher = build_dispatcher(frozenset({ADMIN_ID}))

    await dispatcher.feed_update(bot, text_update(ADMIN_ID, "/start", update_id=99))


async def test_failed_reply_is_logged_without_the_token_in_dev(
    offline_bot: Bot, capsys: pytest.CaptureFixture[str]
) -> None:
    await fail_to_reply(offline_bot, "dev")

    output = capsys.readouterr().out
    assert "unhandled_update_error" in output
    assert TOKEN not in output


@pytest.mark.xfail(
    reason=(
        "QA-T1 F-1 (critical): prod logging uses structlog dict_tracebacks, whose default "
        "show_locals=True renders aiogram's local `url` (contains the bot token)"
    ),
    strict=True,
)
async def test_failed_reply_is_logged_without_the_token_in_prod(
    offline_bot: Bot, capsys: pytest.CaptureFixture[str]
) -> None:
    await fail_to_reply(offline_bot, "prod")

    output = capsys.readouterr().out
    assert "unhandled_update_error" in output
    assert TOKEN not in output
