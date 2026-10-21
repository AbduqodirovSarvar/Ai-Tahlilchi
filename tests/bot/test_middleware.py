"""Tests for app.bot.middleware."""

from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Chat, Update, User
from hypothesis import given
from hypothesis import strategies as st

from app.bot.middleware import WhitelistMiddleware
from tests.bot.fakes import ADMIN_ID, STRANGER_ID

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
