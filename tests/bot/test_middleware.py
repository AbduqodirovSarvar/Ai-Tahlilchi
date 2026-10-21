"""Tests for app.bot.middleware."""

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import Chat, Update, User
from hypothesis import given
from hypothesis import strategies as st

from app.bot import texts
from app.bot.draft import StaleDraftError
from app.bot.messaging import answer_callback
from app.bot.middleware import (
    ErrorMiddleware,
    RateLimitMiddleware,
    WhitelistMiddleware,
    _plain,
    user_message_for,
)
from app.bot.parsing import InputError
from app.services.errors import (
    BudgetExceededError,
    CooldownActiveError,
    NotFoundError,
    ValidationFailedError,
)
from tests.bot.fakes import (
    ADMIN_ID,
    STRANGER_ID,
    RecordingSession,
    bind,
    callback_update,
    text_update,
)

WHITELIST = frozenset({ADMIN_ID})


def user(user_id: int) -> User:
    return User(id=user_id, is_bot=False, first_name="Test")


def private_chat_with(user_id: int) -> dict[str, Any]:
    """Middleware data as aiogram resolves it for a private chat message."""
    return {"event_from_user": user(user_id), "event_chat": Chat(id=user_id, type="private")}


async def call(middleware: WhitelistMiddleware, data: dict[str, Any]) -> AsyncMock:
    handler = AsyncMock(return_value="handled")
    result = await middleware(handler, Update(update_id=1), data)
    if handler.await_count:
        assert result == "handled"
    else:
        assert result is None
    return handler


async def test_whitelisted_user_reaches_handler() -> None:
    handler = await call(WhitelistMiddleware(WHITELIST), private_chat_with(ADMIN_ID))
    handler.assert_awaited_once()


async def test_stranger_is_dropped_silently() -> None:
    handler = await call(WhitelistMiddleware(WHITELIST), private_chat_with(STRANGER_ID))
    handler.assert_not_awaited()


async def test_update_without_user_is_dropped() -> None:
    handler = await call(WhitelistMiddleware(WHITELIST), {})
    handler.assert_not_awaited()


async def test_empty_whitelist_rejects_everyone() -> None:
    handler = await call(WhitelistMiddleware(frozenset()), private_chat_with(ADMIN_ID))
    handler.assert_not_awaited()


@given(
    whitelist=st.frozensets(st.integers(min_value=1, max_value=10**12), max_size=5),
    user_id=st.integers(min_value=1, max_value=10**12),
)
async def test_passes_exactly_whitelisted_users(whitelist: frozenset[int], user_id: int) -> None:
    handler = await call(WhitelistMiddleware(whitelist), private_chat_with(user_id))
    assert handler.await_count == (1 if user_id in whitelist else 0)


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
async def test_admin_outside_a_private_chat_is_dropped(chat_type: str) -> None:
    data = {"event_from_user": user(ADMIN_ID), "event_chat": Chat(id=-100, type=chat_type)}

    handler = await call(WhitelistMiddleware(WHITELIST), data)

    handler.assert_not_awaited()


async def test_admin_update_without_a_chat_is_dropped() -> None:
    handler = await call(WhitelistMiddleware(WHITELIST), {"event_from_user": user(ADMIN_ID)})
    handler.assert_not_awaited()


# --------------------------------------------------------------------------- rate limit
class FakeMonotonic:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_rate_limit_allows_burst_then_warns_once(bot: Bot, session: RecordingSession) -> None:
    clock = FakeMonotonic()
    limiter = RateLimitMiddleware(capacity=2, refill_per_second=1.0, monotonic=clock)
    handler = AsyncMock(return_value="ok")
    update = bind(text_update(ADMIN_ID, "salom"), bot)
    data = {"event_from_user": user(ADMIN_ID)}

    results = [await limiter(handler, update, data) for _ in range(4)]

    assert results == ["ok", "ok", None, None]
    sent = [m for m in session.requests if isinstance(m, SendMessage)]
    assert [m.text for m in sent] == [texts.RATE_LIMITED]
    clock.now = 1.0
    assert await limiter(handler, update, data) == "ok"


async def test_rate_limit_answers_throttled_buttons(bot: Bot, session: RecordingSession) -> None:
    limiter = RateLimitMiddleware(capacity=1, refill_per_second=0.0, monotonic=FakeMonotonic())
    handler = AsyncMock()
    update = bind(callback_update(ADMIN_ID, "x", 1), bot)
    data = {"event_from_user": user(ADMIN_ID)}

    await limiter(handler, update, data)
    await limiter(handler, update, data)

    assert handler.await_count == 1
    assert isinstance(session.requests[-1], AnswerCallbackQuery)


# --------------------------------------------------------------------------- errors
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (InputError("typed wrong"), "typed wrong"),
        (StaleDraftError(), texts.STALE_BUTTON_HINT),
        (CooldownActiveError(timedelta(minutes=3)), "⏳ 3 daqiqadan"),
        (BudgetExceededError(10, 2), "10 ta so'rov kerak"),
        (ValidationFailedError("markets", "x"), "bozorni"),
        (NotFoundError("signal", 1), "Topilmadi"),
        (RuntimeError("bug"), texts.UNEXPECTED_ERROR),
    ],
)
def test_user_message_for_each_error(error: Exception, expected: str) -> None:
    assert expected in user_message_for(error)


async def test_error_middleware_reports_long_callback_errors_as_message(
    bot: Bot, session: RecordingSession
) -> None:
    update = bind(callback_update(ADMIN_ID, "x", 1), bot)
    assert update.callback_query is not None
    handler = AsyncMock(side_effect=InputError("x" * 300))

    assert await ErrorMiddleware()(handler, update.callback_query, {}) is None

    kinds = [type(m).__name__ for m in session.requests]
    assert kinds == ["AnswerCallbackQuery", "SendMessage"]


async def test_error_after_the_callback_was_answered_still_reaches_the_user(
    bot: Bot, session: RecordingSession
) -> None:
    """A handler that answered ("Saqlandi") and then failed: the alert would be rejected."""
    update = bind(callback_update(ADMIN_ID, "x", 1), bot)
    callback = update.callback_query
    assert callback is not None

    async def answers_then_fails(_event: Any, _data: dict[str, Any]) -> None:
        await answer_callback(callback, texts.SETTINGS_SAVED)
        raise NotFoundError("signal", 1)

    assert await ErrorMiddleware()(answers_then_fails, callback, {}) is None

    kinds = [type(m).__name__ for m in session.requests]
    assert kinds == ["AnswerCallbackQuery", "SendMessage"]
    assert session.requests[1].text == texts.NOT_FOUND  # type: ignore[attr-defined]


async def test_error_before_any_answer_is_an_alert(bot: Bot, session: RecordingSession) -> None:
    update = bind(callback_update(ADMIN_ID, "x", 1), bot)
    assert update.callback_query is not None
    handler = AsyncMock(side_effect=NotFoundError("signal", 1))

    await ErrorMiddleware()(handler, update.callback_query, {})

    assert [type(m).__name__ for m in session.requests] == ["AnswerCallbackQuery"]


async def test_error_middleware_survives_telegram_failure(
    bot: Bot, monkeypatch: pytest.MonkeyPatch
) -> None:
    update = bind(text_update(ADMIN_ID, "x"), bot)
    assert update.message is not None
    bad = TelegramBadRequest(method=SendMessage(chat_id=1, text="x"), message="bad")
    monkeypatch.setattr("app.bot.middleware.send_html", AsyncMock(side_effect=bad))
    handler = AsyncMock(side_effect=NotFoundError("x", 1))

    assert await ErrorMiddleware()(handler, update.message, {}) is None


def test_alert_text_is_plain() -> None:
    assert _plain("<b>a</b> &lt;b&gt; <code>c</code> &amp;") == "a <b> c &"
