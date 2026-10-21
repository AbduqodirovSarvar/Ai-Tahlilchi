"""End-to-end (offline) tests of the assembled dispatcher: middleware + routers + handlers."""

from unittest.mock import MagicMock

import pytest
from aiogram import Bot, Router
from aiogram.methods import SendMessage
from aiogram.types import ErrorEvent, Message, Update

from app.bot import dispatcher as dispatcher_module
from app.bot import texts
from app.bot.dispatcher import build_dispatcher, log_unhandled_error
from tests.bot.fakes import ADMIN_ID, STRANGER_ID, RecordingSession, text_update

WHITELIST = frozenset({ADMIN_ID})


async def test_start_replies_to_admin(bot: Bot, session: RecordingSession) -> None:
    dispatcher = build_dispatcher(WHITELIST)

    await dispatcher.feed_update(bot, text_update(ADMIN_ID, "/start"))

    assert len(session.requests) == 1
    request = session.requests[0]
    assert isinstance(request, SendMessage)
    assert request.chat_id == ADMIN_ID
    assert request.text == texts.START_OK


async def test_start_from_stranger_gets_no_reply(bot: Bot, session: RecordingSession) -> None:
    dispatcher = build_dispatcher(WHITELIST)

    await dispatcher.feed_update(bot, text_update(STRANGER_ID, "/start"))

    assert session.requests == []


async def test_unknown_text_is_ignored(bot: Bot, session: RecordingSession) -> None:
    dispatcher = build_dispatcher(WHITELIST)

    await dispatcher.feed_update(bot, text_update(ADMIN_ID, "salom"))

    assert session.requests == []


async def test_handler_crash_is_contained_and_logged(
    bot: Bot, session: RecordingSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    logger = MagicMock()
    monkeypatch.setattr(dispatcher_module, "logger", logger)
    dispatcher = build_dispatcher(WHITELIST)
    crashing = Router(name="crashing")

    @crashing.message()
    async def explode(message: Message) -> None:
        raise RuntimeError(message.text)

    dispatcher.include_router(crashing)

    # Must not raise: the global error handler swallows and logs it.
    await dispatcher.feed_update(bot, text_update(ADMIN_ID, "boom", update_id=77))

    assert session.requests == []
    logger.error.assert_called_once()
    assert logger.error.call_args.kwargs["update_id"] == 77
    assert isinstance(logger.error.call_args.kwargs["exc_info"], RuntimeError)


async def test_error_handler_marks_error_as_handled() -> None:
    event = ErrorEvent(update=Update(update_id=5), exception=ValueError("bad"))

    assert await log_unhandled_error(event) is True


def test_dispatcher_listens_only_to_used_update_types() -> None:
    assert build_dispatcher(WHITELIST).resolve_used_update_types() == ["message"]
