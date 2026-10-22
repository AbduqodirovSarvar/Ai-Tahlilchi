"""End-to-end (offline) tests of the assembled dispatcher: middleware + routers + handlers."""

from unittest.mock import MagicMock

import pytest
from aiogram import Bot, Router
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage
from aiogram.types import ErrorEvent, Message, ReplyKeyboardMarkup, Update

from app.bot import dispatcher as dispatcher_module
from app.bot import keyboards, texts
from app.bot.dispatcher import build_dispatcher, log_unhandled_error, utc_now
from app.services.fake import create_fake_services
from tests.bot.fakes import (
    ADMIN_ID,
    STRANGER_ID,
    BotHarness,
    RecordingSession,
    sent_texts,
    text_update,
)

WHITELIST = frozenset({ADMIN_ID})


async def test_start_shows_welcome_and_main_menu(bot: Bot, session: RecordingSession) -> None:
    dispatcher = build_dispatcher(WHITELIST, create_fake_services())

    await dispatcher.feed_update(bot, text_update(ADMIN_ID, "/start"))

    assert len(session.requests) == 1
    request = session.requests[0]
    assert isinstance(request, SendMessage)
    assert request.chat_id == ADMIN_ID
    assert request.text == texts.WELCOME
    assert request.parse_mode == "HTML"
    assert isinstance(request.reply_markup, ReplyKeyboardMarkup)


def test_main_menu_matches_plan_layout() -> None:
    rows = [[button.text for button in row] for row in keyboards.main_menu().keyboard]
    assert rows == [
        ["📊 Tahlil", "🔍 Maxsus tahlil"],
        ["💾 Shablonlarim", "⚡ Bugungi signallar"],
        ["⭐ Sevimlilar", "🎟 Mening stavkalarim"],
        ["📈 Hisobot", "⚙️ Sozlamalar"],
        ["ℹ️ Holat / Yordam"],
    ]


async def test_start_from_stranger_gets_no_reply(bot: Bot, session: RecordingSession) -> None:
    dispatcher = build_dispatcher(WHITELIST, create_fake_services())

    await dispatcher.feed_update(bot, text_update(STRANGER_ID, "/start"))

    assert session.requests == []


@pytest.mark.parametrize("command", ["/yordam", "/help"])
async def test_help_lists_commands_and_glossary(harness: BotHarness, command: str) -> None:
    methods = await harness.send(command)

    text = sent_texts(methods)[0]
    for term in ("/tahlil", "/sozlamalar", "/bekor", "EV", "CLV", "Unit"):
        assert term in text


async def test_unknown_text_gets_a_hint(harness: BotHarness) -> None:
    methods = await harness.send("salom")

    assert sent_texts(methods) == [texts.UNKNOWN_TEXT]


async def test_cancel_without_flow_and_inside_flow(harness: BotHarness) -> None:
    assert sent_texts(await harness.send("/bekor")) == [texts.NOTHING_TO_CANCEL]

    await harness.send(texts.MENU_ANALYSIS)
    message_id, _ = harness.find_button(texts.BTN_TODAY)
    assert sent_texts(await harness.send("/bekor")) == [texts.CANCELLED]
    assert harness.markups[message_id] is None


async def test_start_inside_a_flow_closes_it(harness: BotHarness) -> None:
    await harness.send(texts.MENU_ANALYSIS)
    await harness.send("/start")

    assert sent_texts(await harness.send("22.10")) == [texts.UNKNOWN_TEXT]


async def test_handler_crash_is_reported_and_logged(
    bot: Bot, session: RecordingSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    logger = MagicMock()
    monkeypatch.setattr("app.bot.middleware.logger", logger)
    dispatcher = build_dispatcher(WHITELIST, create_fake_services())
    crashing = Router(name="crashing")

    @crashing.message()
    async def explode(message: Message) -> None:
        raise RuntimeError(message.text)

    # Routers are matched in order; put the crashing one before the catch-all.
    dispatcher.include_router(crashing)
    dispatcher.sub_routers.insert(0, dispatcher.sub_routers.pop())

    await dispatcher.feed_update(bot, text_update(ADMIN_ID, "boom", update_id=77))

    assert sent_texts(session.requests) == [texts.UNEXPECTED_ERROR]
    logger.exception.assert_called_once()


async def test_error_handler_marks_error_as_handled(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = MagicMock()
    monkeypatch.setattr(dispatcher_module, "logger", logger)
    event = ErrorEvent(update=Update(update_id=5), exception=ValueError("bad"))

    assert await log_unhandled_error(event) is True
    assert logger.error.call_args.kwargs["update_id"] == 5


def test_dispatcher_listens_to_messages_and_buttons() -> None:
    dispatcher = build_dispatcher(WHITELIST, create_fake_services())
    assert dispatcher.resolve_used_update_types() == ["callback_query", "message"]


def test_custom_storage_is_used() -> None:
    storage = MemoryStorage()
    dispatcher = build_dispatcher(WHITELIST, create_fake_services(), storage=storage)
    assert dispatcher.storage is storage


def test_default_clock_is_utc() -> None:
    assert utc_now().utcoffset() is not None
