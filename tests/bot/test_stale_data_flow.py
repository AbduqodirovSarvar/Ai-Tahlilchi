"""``DataStaleError`` on every analysis path: a clear text and the refresh button right there."""

from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot

from app.bot import texts
from app.bot.callbacks import CardCb
from app.bot.middleware import error_reply, user_message_for
from app.bot.refresh_ui import format_data_stale
from app.services.errors import DataStaleError
from app.services.fake import create_fake_services
from tests.bot.browse_samples import WithOverrides
from tests.bot.fakes import ADMIN_ID, BotHarness, RecordingSession, fixed_clock, sent_texts
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


async def test_wizard_run_reports_a_stale_database(bot: Bot, session: RecordingSession) -> None:
    harness = stale_harness(bot, session, analyze=raising(STALE))
    await harness.send("/tahlil 7kun 50% ev3%")

    await harness.press(texts.BTN_START)

    assert_stale_notice(harness, harness.last_text)
    harness.find_button(texts.BTN_START)  # the confirmation is given back for a retry


async def test_recalc_window_reports_a_stale_database(bot: Bot, session: RecordingSession) -> None:
    harness = stale_harness(bot, session, recalc_window=raising(STALE))

    methods = await harness.send("/qayta")

    assert_stale_notice(harness, sent_texts(methods)[-1])


async def test_card_recalc_button_reports_through_the_error_middleware(
    bot: Bot, session: RecordingSession
) -> None:
    harness = stale_harness(bot, session, recalc_signal=raising(STALE))
    await harness.send("/tahlil 7kun 50% ev3%")
    await harness.press(texts.BTN_START)
    message_id, data = harness.find_button(texts.BTN_HIDE)
    signal_id = CardCb.unpack(data).signal_id

    methods = await harness.press_data(
        CardCb(action="recalc", signal_id=signal_id).pack(), message_id
    )

    assert_stale_notice(harness, sent_texts(methods)[-1])


async def test_confirmation_screen_warns_and_hides_start_when_the_estimate_is_stale(
    bot: Bot, session: RecordingSession
) -> None:
    services = create_fake_services(clock=fixed_clock)

    async def estimate(user_id: int, query: object):
        base = await services.analysis.estimate_cost(user_id, query)  # type: ignore[arg-type]
        return replace(base, data_stale=True, last_refresh_at=LAST)

    wrapped = replace(services, analysis=WithOverrides(services.analysis, estimate_cost=estimate))  # type: ignore[arg-type]
    harness = BotHarness(bot, session, wrapped)

    methods = await harness.send("/tahlil 7kun 50% ev3%")

    assert_stale_notice(harness, sent_texts(methods)[-1])
    with pytest.raises(AssertionError):
        harness.find_button(texts.BTN_START)
    harness.find_button(texts.BTN_EDIT)


async def test_refresh_from_the_stale_notice_runs_and_reports(
    bot: Bot, session: RecordingSession
) -> None:
    harness = stale_harness(bot, session, recalc_window=raising(STALE))
    await harness.send("/qayta")

    methods = await harness.press(texts.BTN_DB_REFRESH)

    assert "Baza yangilandi" in sent_texts(methods)[-1]
