"""``DataStaleError`` on every analysis path: a clear text and the refresh button right there."""

from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot

from app.bot import texts
from app.bot.middleware import error_reply, user_message_for
from app.bot.refresh_ui import format_data_stale
from app.services.errors import DataStaleError
from app.services.fake import create_fake_services
from tests.bot.browse_samples import WithOverrides
from tests.bot.fakes import ADMIN_ID, BotHarness, RecordingSession, fixed_clock
from tests.e2e.bot_client import telegram_html_problems

LAST = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)  # 20:00 in Tashkent
STALE = DataStaleError(LAST)


def stale_harness(bot: Bot, session: RecordingSession, **overrides: object) -> BotHarness:
    services = create_fake_services(clock=fixed_clock)
    wrapped = replace(services, analysis=WithOverrides(services.analysis, **overrides))  # type: ignore[arg-type]
    return BotHarness(bot, session, wrapped)


def raising(error: Exception):
    async def fail(*_args: object, **_kwargs: object) -> None:
        raise error

    return fail


def assert_stale_notice(harness: BotHarness, text: str) -> None:
    assert "Baza yangilanmagan" in text
    assert "oxirgi: 20.10 20:00" in text
    assert "Bazani yangilash" in text
    assert telegram_html_problems(text) == []
    harness.find_button(texts.BTN_DB_REFRESH)


def test_text_names_the_last_refresh_in_the_users_zone_or_never() -> None:
    assert "oxirgi: 20.10 20:00" in format_data_stale(LAST, ZoneInfo("Asia/Tashkent"))
    assert "oxirgi: 20.10 15:00" in format_data_stale(LAST, ZoneInfo("UTC"))
    assert "hali yangilanmagan" in format_data_stale(None, ZoneInfo("UTC"))
    assert "Bazani yangilash" in user_message_for(STALE)


async def test_error_reply_carries_the_refresh_button_only_when_stale() -> None:
    services = create_fake_services(clock=fixed_clock)

    stale = await error_reply(STALE, services, ADMIN_ID)
    other = await error_reply(RuntimeError("x"), services, ADMIN_ID)

    assert "oxirgi: 20.10 20:00" in stale.text
    assert stale.markup is not None
    assert other.markup is None
