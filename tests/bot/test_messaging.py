"""Tests for app.bot.messaging: flood control, splitting and tolerant edits/answers."""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, User

from app.bot.messaging import (
    MAX_RETRY_AFTER_SECONDS,
    FloodControlMiddleware,
    answer_callback,
    drop_keyboard,
    edit_html,
    install_flood_control,
    send_html,
)
from tests.bot.fakes import RecordingSession

METHOD = SendMessage(chat_id=1, text="x")


def flood(retry_after: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(method=METHOD, message="Too Many Requests", retry_after=retry_after)


async def test_flood_control_retries_once_after_waiting(bot: Bot) -> None:
    sleep = AsyncMock()
    make_request = AsyncMock(side_effect=[flood(3), "ok"])

    result = await FloodControlMiddleware(sleep)(make_request, bot, METHOD)

    assert result == "ok"
    sleep.assert_awaited_once_with(3.0)
    assert make_request.await_count == 2


async def test_flood_control_gives_up_after_second_flood(bot: Bot) -> None:
    make_request = AsyncMock(side_effect=[flood(1), flood(1)])
    with pytest.raises(TelegramRetryAfter):
        await FloodControlMiddleware(AsyncMock())(make_request, bot, METHOD)


async def test_flood_control_does_not_sleep_through_long_waits(bot: Bot) -> None:
    sleep = AsyncMock()
    make_request = AsyncMock(side_effect=flood(MAX_RETRY_AFTER_SECONDS + 1))
    with pytest.raises(TelegramRetryAfter):
        await FloodControlMiddleware(sleep)(make_request, bot, METHOD)
    sleep.assert_not_awaited()


class FloodOnceSession(RecordingSession):
    """Fails the first request with flood control, then records like the parent."""

    def __init__(self) -> None:
        super().__init__()
        self.flooded = False

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109 - aiogram's signature
    ) -> Any:
        if not self.flooded:
            self.flooded = True
            raise flood(1)
        return await super().make_request(bot, method, timeout)


async def test_installed_middleware_covers_every_call() -> None:
    session = FloodOnceSession()
    bot = Bot(token="42:TEST", session=session)
    sleep = AsyncMock()
    install_flood_control(bot, sleep)

    await bot.send_message(chat_id=1, text="hi")

    sleep.assert_awaited_once()
    assert len(session.requests) == 1


def message(bot: Bot) -> Message:
    return Message(
        message_id=5, date=datetime.now(UTC), chat=Chat(id=1, type="private"), text="x"
    ).as_(bot)


async def test_send_html_splits_long_text(bot: Bot, session: RecordingSession) -> None:
    await send_html(message(bot), "a\n" * 5000)

    sent = [m for m in session.requests if isinstance(m, SendMessage)]
    assert len(sent) == 3
    assert all(len(m.text) <= 4096 for m in sent)
    assert all(m.parse_mode == "HTML" for m in sent)


async def test_edit_ignores_not_modified(bot: Bot, monkeypatch: pytest.MonkeyPatch) -> None:
    target = message(bot)
    error = TelegramBadRequest(method=METHOD, message="Bad Request: message is not modified")
    monkeypatch.setattr(Message, "edit_text", AsyncMock(side_effect=error))
    await edit_html(target, "same")

    other = TelegramBadRequest(method=METHOD, message="Bad Request: chat not found")
    monkeypatch.setattr(Message, "edit_text", AsyncMock(side_effect=other))
    with pytest.raises(TelegramBadRequest):
        await edit_html(target, "x")


async def test_answer_callback_truncates_and_ignores_old_queries(
    bot: Bot, session: RecordingSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    callback = CallbackQuery(
        id="1", from_user=User(id=1, is_bot=False, first_name="T"), chat_instance="c", data="x"
    ).as_(bot)
    await answer_callback(callback, "y" * 500, alert=True)
    answer = session.requests[-1]
    assert isinstance(answer, AnswerCallbackQuery)
    assert answer.text is not None
    assert len(answer.text) == 200

    old = TelegramBadRequest(method=METHOD, message="query is too old")
    monkeypatch.setattr(CallbackQuery, "answer", AsyncMock(side_effect=old))
    await answer_callback(callback)  # must not raise


async def test_drop_keyboard_is_best_effort(bot: Bot, session: RecordingSession) -> None:
    await drop_keyboard(bot, 1, None)
    assert session.requests == []
    await drop_keyboard(bot, 1, 7)
    assert isinstance(session.requests[-1], EditMessageReplyMarkup)

    failing = Bot(token="42:TEST", session=FloodOnceSession())
    await drop_keyboard(failing, 1, 7)  # flood error is swallowed (no middleware installed)
