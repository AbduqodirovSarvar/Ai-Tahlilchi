"""🗄 Baza through the real dispatcher over the fake services."""

import asyncio
from dataclasses import replace

import pytest
from aiogram import Bot

from app.bot import texts
from app.bot.callbacks import DbCb
from app.services.errors import (
    BudgetExceededError,
    CooldownActiveError,
    ServiceUnavailableError,
)
from app.services.fake import create_fake_services
from tests.bot.browse_samples import WithOverrides, make_overview
from tests.bot.fakes import (
    ADMIN_ID,
    BotHarness,
    RecordingSession,
    answers,
    fixed_clock,
    sent_texts,
)
from tests.e2e.bot_client import telegram_html_problems


def browser_harness(bot: Bot, session: RecordingSession, **overrides: object) -> BotHarness:
    """A harness whose browser service delegates to the fake except for ``overrides``."""
    services = create_fake_services(clock=fixed_clock)
    wrapped = replace(services, browser=WithOverrides(services.browser, **overrides))  # type: ignore[arg-type]
    return BotHarness(bot, session, wrapped)


async def press_exact(harness: BotHarness, label: str) -> None:
    """Tap the button whose text is exactly ``label`` (``press`` matches substrings)."""
    found = [
        (message_id, button.callback_data)
        for message_id, markup in sorted(harness.markups.items(), reverse=True)
        if markup
        for row in markup.inline_keyboard
        for button in row
        if button.text == label and button.callback_data
    ]
    assert found, label
    await harness.press_data(found[0][1], found[0][0])


async def test_baza_command_and_menu_button_open_the_overview(harness: BotHarness) -> None:
    first = await harness.send("/baza")
    second = await harness.send(texts.MENU_DB)

    for methods in (first, second):
        text = sent_texts(methods)[0]
        assert "Jadvallar" in text
        assert "Asia/Tashkent" in text
        assert telegram_html_problems(text) == []
    harness.find_button(texts.BTN_DB_LEAGUES)
    harness.find_button(texts.BTN_DB_REFRESH)


async def test_leagues_to_fixture_card_to_prices_and_all_the_way_back(
    harness: BotHarness,
) -> None:
    await harness.send("/baza")
    await harness.press(texts.BTN_DB_LEAGUES)
    assert "Ligalar" in harness.last_text
    await harness.press("1")
    assert "Bundesliga</b> — 22 ta o'yin" in harness.last_text
    await harness.press("1")
    assert "Koeffitsientlar" in harness.last_text
    await harness.press(texts.BTN_DB_ODDS)
    assert "Narxlar tarixi" in harness.last_text
    await harness.press(texts.BTN_DB_BACK_CARD)
    assert "Koeffitsientlar" in harness.last_text
    await harness.press(texts.BTN_DB_BACK_FIXTURES)
    assert "22 ta o'yin" in harness.last_text
    await harness.press(texts.BTN_DB_BACK_LEAGUES)
    assert "bazada 5 ta" in harness.last_text


async def test_every_tap_is_answered(harness: BotHarness) -> None:
    await harness.send("/baza")
    methods = await harness.press(texts.BTN_DB_LEAGUES)

    assert len(answers(methods)) == 1


async def test_all_fixtures_with_the_day_filter(harness: BotHarness) -> None:
    await harness.send("/baza")
    await harness.press(texts.BTN_DB_ALL)
    assert "Barcha kunlar" in harness.last_text
    await harness.press(texts.BTN_TODAY)
    assert "Kun: <b>21.10.2026</b>" in harness.last_text
    assert "21.10, Chorshanba" in harness.last_text
    assert "20.10" not in harness.last_text
    await harness.press(texts.BTN_TOMORROW)
    assert "Kun: <b>22.10.2026</b>" in harness.last_text
    await harness.press(texts.BTN_DB_DAY_ALL)
    assert "Barcha kunlar" in harness.last_text


async def test_pager_walks_pages_and_a_repeated_tap_is_harmless(harness: BotHarness) -> None:
    await harness.send("/baza")
    await harness.press(texts.BTN_DB_ALL)
    message_id, next_page = harness.find_button(texts.BTN_NEXT_PAGE)

    await harness.press_data(next_page, message_id)
    assert "2/" in str(harness.markups[message_id])
    again = await harness.press_data(next_page, message_id)

    assert "2/" in str(harness.markups[message_id])
    assert len(answers(again)) == 1


async def test_calendar_picks_a_day_and_goes_back(harness: BotHarness) -> None:
    await harness.send("/baza")
    await harness.press(texts.BTN_DB_ALL)
    await harness.press(texts.BTN_DB_DAY_PICK)
    assert harness.last_text == texts.DB_CALENDAR_PROMPT
    await press_exact(harness, "20")
    assert "Kun: <b>20.10.2026</b>" in harness.last_text
    await harness.press(texts.BTN_DB_DAY_PICK)
    await harness.press(texts.BTN_BACK)
    assert "Kun: <b>20.10.2026</b>" in harness.last_text


async def test_teams_list_pick_and_typed_search(harness: BotHarness) -> None:
    await harness.send("/baza")
    await harness.press(texts.BTN_DB_TEAMS)
    assert "Jamoalar" in harness.last_text
    await harness.press("1")
    assert "o'yin" in harness.last_text
    await harness.press(texts.BTN_DB_BACK_TEAMS)

    prompt = await harness.press(texts.BTN_DB_SEARCH)
    assert "Jamoa nomini yozing" in sent_texts(prompt)[-1]
    short = await harness.send("a")
    assert "kamida" in sent_texts(short)[-1].lower()
    found = await harness.send("  arsenal ")
    assert "Qidiruv: <b>arsenal</b>" in sent_texts(found)[-1]
    await harness.press(texts.BTN_DB_SEARCH)
    missing = await harness.send("zzzz")
    assert "topilmadi" in sent_texts(missing)[-1]


async def test_search_text_too_long_is_refused(harness: BotHarness) -> None:
    await harness.send("/baza")
    await harness.press(texts.BTN_DB_TEAMS)
    await harness.press(texts.BTN_DB_SEARCH)

    methods = await harness.send("x" * 65)

    assert "oshmasin" in sent_texts(methods)[-1]


async def test_buttons_of_an_older_screen_are_stale(harness: BotHarness) -> None:
    await harness.send("/baza")
    await harness.press(texts.BTN_DB_LEAGUES)
    old_id, old_pick = harness.find_button("1")
    await harness.send("/baza")
    await harness.press(texts.BTN_DB_TEAMS)

    methods = await harness.press_data(old_pick, old_id)

    assert answers(methods)[0].text == texts.DB_STALE_SCREEN
    assert answers(methods)[0].show_alert is True


async def test_buttons_after_a_restart_are_stale_not_silent(harness: BotHarness) -> None:
    await harness.send("/baza")
    await harness.press(texts.BTN_DB_LEAGUES)
    message_id, data = harness.find_button("1")
    harness.restart()

    methods = await harness.press_data(data, message_id)

    assert answers(methods)[0].text == texts.DB_STALE_SCREEN


async def test_a_button_of_another_screen_only_redraws_the_current_one(
    harness: BotHarness,
) -> None:
    await harness.send("/baza")
    await harness.press(texts.BTN_DB_LEAGUES)
    message_id, pick = harness.find_button("1")
    await harness.press_data(pick, message_id)  # now on the fixtures screen
    shown = harness.last_text

    await harness.press_data(pick, message_id)  # the same (leagues) button again

    assert harness.last_text == shown


async def test_empty_database_explains_and_offers_the_refresh(
    bot: Bot, session: RecordingSession
) -> None:
    empty = make_overview(
        tables=(), earliest_kickoff=None, latest_kickoff=None, last_refresh_at=None
    )

    async def overview(_user_id: int):
        return empty

    harness = browser_harness(bot, session, database_overview=overview)
    methods = await harness.send("/baza")

    assert "Baza bo'sh" in sent_texts(methods)[0]
    harness.find_button(texts.BTN_DB_REFRESH)
    with pytest.raises(AssertionError):
        harness.find_button(texts.BTN_DB_LEAGUES)


# --------------------------------------------------------------------------- refresh
async def test_refresh_reports_what_was_stored_then_cooldown(harness: BotHarness) -> None:
    await harness.send("/baza")
    first = await harness.press(texts.BTN_DB_REFRESH)

    assert "Baza yangilanmoqda" in sent_texts(first)[0]
    assert "Baza yangilandi" in sent_texts(first)[-1]
    assert "O'yinlar: <b>12</b>" in sent_texts(first)[-1]
    second = await harness.press(texts.BTN_DB_REFRESH)
    assert "yana 30 daqiqadan keyin" in sent_texts(second)[-1]


async def test_refresh_refused_for_non_admin(bot: Bot, session: RecordingSession) -> None:
    services = create_fake_services(clock=fixed_clock, admin_ids=frozenset({42}))
    harness = BotHarness(bot, session, services)
    await harness.send("/baza")

    methods = await harness.press(texts.BTN_DB_REFRESH)

    assert sent_texts(methods)[-1] == texts.NOT_AUTHORIZED


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (BudgetExceededError(40, 12), "kerak ≈40 ta, qoldi 12 ta"),
        (ServiceUnavailableError("api-football"), "api-football"),
        (CooldownActiveError(__import__("datetime").timedelta(minutes=7, seconds=1)), "8 daqiqa"),
        (RuntimeError("boom"), texts.UNEXPECTED_ERROR),
    ],
)
async def test_refresh_failures_get_a_clear_message(
    bot: Bot, session: RecordingSession, error: Exception, expected: str
) -> None:
    async def refresh(_user_id: int):
        raise error

    harness = browser_harness(bot, session, refresh_database=refresh)
    await harness.send("/baza")

    methods = await harness.press(texts.BTN_DB_REFRESH)

    assert expected in sent_texts(methods)[-1]
    harness.find_button(texts.BTN_DB_HOME)


async def test_a_second_tap_during_a_running_refresh_is_refused(
    bot: Bot, session: RecordingSession
) -> None:
    gate = asyncio.Event()
    inner = create_fake_services(clock=fixed_clock).browser
    calls = []

    async def refresh(user_id: int):
        calls.append(user_id)
        await gate.wait()
        return await inner.refresh_database(user_id)

    harness = browser_harness(bot, session, refresh_database=refresh)
    await harness.send("/baza")
    message_id, data = harness.find_button(texts.BTN_DB_REFRESH)
    harness.update_id += 1
    first = asyncio.create_task(harness.press_data(data, message_id))
    await asyncio.sleep(0.05)

    second = await harness.press_data(data, message_id)
    gate.set()
    await first

    assert texts.DB_REFRESH_ALREADY in [a.text for a in answers(second)]
    assert calls == [ADMIN_ID]


def test_refresh_callbacks_fit_the_limit() -> None:
    assert len(DbCb(action="refresh").pack().encode()) <= 64
