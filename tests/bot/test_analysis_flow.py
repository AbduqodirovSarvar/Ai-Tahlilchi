"""End-to-end (offline) tests of the 📊 Tahlil wizard through the real dispatcher."""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from aiogram.methods import EditMessageText, SendMessage

from app.bot import texts
from app.bot.callbacks import CalendarCb, PickCb
from app.services.errors import BudgetExceededError, ServiceUnavailableError
from app.services.interfaces import AnalysisResult, CostEstimate, DateWindow, SortKey
from tests.bot.fakes import FIXED_NOW, BotHarness, answers, sent_texts

TODAY = FIXED_NOW.date()


async def run_to_confirm(harness: BotHarness, probability: str = "50%") -> None:
    await harness.send(texts.MENU_ANALYSIS)
    await harness.press(texts.BTN_TODAY)
    await harness.press("Futbol")
    await harness.press(probability)
    await harness.press("5%")
    await harness.press(texts.BTN_SCOPE_ALL)
    await harness.press(texts.BTN_ALL)
    await harness.press(texts.BTN_DONE)
    await harness.press(texts.SORT_LABELS[SortKey.EV])


async def test_menu_button_opens_the_date_step(harness: BotHarness) -> None:
    methods = await harness.send(texts.MENU_ANALYSIS)

    assert "Sana oralig'i" in sent_texts(methods)[0]
    for label in (texts.BTN_TODAY, texts.BTN_TOMORROW, texts.BTN_WEEKEND, texts.BTN_CALENDAR):
        harness.find_button(label)
    harness.find_button(texts.BTN_BACK)
    harness.find_button(texts.BTN_CANCEL)


async def test_happy_path_reaches_results(harness: BotHarness) -> None:
    await run_to_confirm(harness)
    assert "So'rov" in harness.last_text
    assert "Taxminiy API sarfi" in harness.last_text

    methods = await harness.press(texts.BTN_START)

    assert len(answers(methods)) == 1
    shown = sent_texts(methods)
    assert shown[0].endswith(texts.WIZARD_STARTED)
    assert shown[1].startswith("⏳")
    assert any(text.startswith("✅ Tahlil tugadi") for text in shown)
    assert any("Natijalar" in text or "Value topilmadi" in text for text in shown)


async def test_every_wizard_button_is_answered_exactly_once(harness: BotHarness) -> None:
    await harness.send(texts.MENU_ANALYSIS)
    for label in (texts.BTN_TOMORROW, "Futbol", "60%", "3%", texts.BTN_SCOPE_ALL, "5"):
        methods = await harness.press(label)
        assert len(answers(methods)) == 1, label


async def test_results_paginate_with_more_button(harness: BotHarness) -> None:
    await harness.send("/tahlil 7kun 50% ev3% 10ta")
    await harness.press(texts.BTN_START)
    first_cards = [t for t in sent_texts(harness.session.requests) if t.startswith("⚽")]

    methods = await harness.press("Yana")

    more_cards = [t for t in sent_texts(methods) if t.startswith("⚽")]
    assert len(first_cards) == 5
    assert 1 <= len(more_cards) <= 5
    assert texts.RESULTS_END.split("(")[0] in sent_texts(methods)[-1]


async def test_back_returns_to_previous_step_and_cancel_ends(harness: BotHarness) -> None:
    await harness.send(texts.MENU_ANALYSIS)
    await harness.press(texts.BTN_TODAY)
    assert "Sport turi" in harness.last_text

    await harness.press(texts.BTN_BACK)
    assert "Sana oralig'i" in harness.last_text

    message_id, today_data = harness.find_button(texts.BTN_TODAY)
    methods = await harness.press(texts.BTN_CANCEL)
    assert sent_texts(methods) == [texts.CANCELLED]
    assert harness.markups[message_id] is None

    # The cancelled wizard's buttons are now stale.
    stale = await harness.press_data(today_data, message_id)
    assert answers(stale)[0].text == texts.STALE_BUTTON


async def test_back_on_first_step_leaves_the_wizard(harness: BotHarness) -> None:
    await harness.send(texts.MENU_ANALYSIS)
    methods = await harness.press(texts.BTN_BACK)
    assert sent_texts(methods) == [texts.CANCELLED]


async def test_default_button_takes_settings_value(harness: BotHarness) -> None:
    await harness.send(texts.MENU_ANALYSIS)
    await harness.press(texts.BTN_TODAY)
    await harness.press("Futbol")
    await harness.press(texts.BTN_DEFAULT)  # probability 45% from settings
    assert "Ehtimollik: 45%" in harness.last_text


async def test_custom_probability_invalid_then_valid(harness: BotHarness) -> None:
    await harness.send(texts.MENU_ANALYSIS)
    await harness.press(texts.BTN_TODAY)
    await harness.press("Futbol")
    await harness.press(texts.BTN_CUSTOM)

    invalid = await harness.send("150%")
    assert "150%" in sent_texts(invalid)[0]
    assert "30–95%" in sent_texts(invalid)[0]

    not_number = await harness.send("ko'p")
    assert "son emas" in sent_texts(not_number)[0]

    valid = await harness.send("65")
    assert "Ehtimollik: 65%" in sent_texts(valid)[-1]


async def test_high_probability_warning_is_shown_once(harness: BotHarness) -> None:
    await harness.send(texts.MENU_ANALYSIS)
    await harness.press(texts.BTN_TODAY)
    await harness.press("Futbol")
    first = await harness.press("90%")
    assert "Diqqat" in sent_texts(first)[0]

    await harness.press(texts.BTN_BACK)
    second = await harness.press("80%")
    assert "Diqqat" not in sent_texts(second)[0]


async def test_typed_date_range_is_accepted(harness: BotHarness) -> None:
    await harness.send(texts.MENU_ANALYSIS)
    methods = await harness.send("22.10 - 24.10")
    assert "Sport turi" in sent_texts(methods)[-1]


@pytest.mark.parametrize(
    ("typed", "expected"),
    [("31.02", "bunday sana yo'q"), ("salom", "tushunmadim"), ("01.12 - 05.12", "uzoqda")],
)
async def test_invalid_typed_dates_explain_the_problem(
    harness: BotHarness, typed: str, expected: str
) -> None:
    await harness.send(texts.MENU_ANALYSIS)
    methods = await harness.send(typed)
    assert expected in sent_texts(methods)[0]


async def test_calendar_picks_start_and_end(harness: BotHarness) -> None:
    await harness.send(texts.MENU_ANALYSIS)
    await harness.press(texts.BTN_CALENDAR)
    assert "Boshlanish" in harness.last_text
    message_id, _ = harness.find_button("Oktabr")
    start = CalendarCb(action="day", value=TODAY.toordinal()).pack()
    end = CalendarCb(action="day", value=(TODAY + timedelta(days=2)).toordinal()).pack()

    await harness.press_data(start, message_id)
    assert "Tugash" in harness.last_text
    await harness.press_data(end, message_id)
    assert "Sport turi" in harness.last_text


async def test_calendar_month_navigation_and_noop(harness: BotHarness) -> None:
    await harness.send(texts.MENU_ANALYSIS)
    await harness.press(texts.BTN_CALENDAR)
    await harness.press(texts.BTN_NEXT_PAGE)
    message_id, _ = harness.find_button("Noyabr 2026")
    methods = await harness.press_data(CalendarCb(action="noop").pack(), message_id)
    assert len(answers(methods)) == 1


async def test_leagues_picker_toggles_and_requires_one(harness: BotHarness) -> None:
    await harness.send(texts.MENU_ANALYSIS)
    await harness.press(texts.BTN_TODAY)
    await harness.press("Futbol")
    await harness.press("50%")
    await harness.press("5%")
    await harness.press(texts.BTN_SCOPE_LEAGUES)

    empty = await harness.press(texts.BTN_DONE)
    assert answers(empty)[0].text == texts.SELECT_AT_LEAST_ONE

    await harness.press("Premier League")
    assert "tanlangan: 1" in harness.last_text
    await harness.press(texts.BTN_DONE)
    assert "O'yinlar soni" in harness.last_text


async def test_teams_search_adds_exact_and_offers_ambiguous(harness: BotHarness) -> None:
    await harness.send(texts.MENU_ANALYSIS)
    await harness.press(texts.BTN_TODAY)
    await harness.press("Futbol")
    await harness.press("50%")
    await harness.press("5%")
    await harness.press(texts.BTN_SCOPE_TEAMS)

    methods = await harness.send("Arsenal, Real, Qwertyzz")

    text = sent_texts(methods)[-1]
    assert "tanlangan: 1" in text
    assert "Qwertyzz" in text
    harness.find_button("Real Madrid")
    await harness.press("Real Madrid")
    assert "tanlangan: 2" in harness.last_text
    await harness.press(texts.BTN_DONE)
    await harness.press("5")
    await harness.press(texts.BTN_DONE)
    await harness.press(texts.SORT_LABELS[SortKey.KICKOFF])
    assert "Arsenal, Real Madrid" in harness.last_text


async def test_fixtures_picker_pages_and_groups_by_day(harness: BotHarness) -> None:
    await harness.send(texts.MENU_ANALYSIS)
    await harness.press(texts.BTN_NEXT_DAYS.format(days=7))
    await harness.press("Futbol")
    await harness.press("50%")
    await harness.press("5%")
    await harness.press(texts.BTN_SCOPE_FIXTURES)
    assert "Sahifa 1/" in harness.last_text
    harness.find_button("📅 Chorshanba")

    await harness.press(texts.BTN_NEXT_PAGE)
    assert "Sahifa 2/" in harness.last_text
    message_id, _ = harness.find_button(texts.BTN_DONE)
    await harness.press_data(PickCb(action="toggle", index=0).pack(), message_id)
    assert "tanlangan: 1" in harness.last_text

    out_of_range = await harness.press_data(PickCb(action="toggle", index=99).pack(), message_id)
    assert "eskirgan" in (answers(out_of_range)[0].text or "")


async def test_count_custom_and_per_day(harness: BotHarness) -> None:
    await harness.send(texts.MENU_ANALYSIS)
    for label in (texts.BTN_TODAY, "Futbol", "50%", "5%", texts.BTN_SCOPE_ALL):
        await harness.press(label)
    await harness.press(texts.BTN_PER_DAY.split(":")[0])
    assert f"Kuniga: {texts.CHECKED}" in harness.last_text
    await harness.press(texts.BTN_CUSTOM)
    invalid = await harness.send("0")
    assert "1–100" in sent_texts(invalid)[0]
    methods = await harness.send("12")
    assert "Bozorlar" in sent_texts(methods)[-1]


async def test_markets_require_at_least_one(harness: BotHarness) -> None:
    await harness.send(texts.MENU_ANALYSIS)
    for label in (texts.BTN_TODAY, "Futbol", "50%", "5%", texts.BTN_SCOPE_ALL, "5"):
        await harness.press(label)
    await harness.press("1X2")
    await harness.press("Total")
    methods = await harness.press(texts.BTN_DONE)
    assert answers(methods)[0].text == texts.SELECT_AT_LEAST_ONE
    await harness.press("Ikkala jamoa")
    await harness.press(texts.BTN_DONE)
    assert "Saralash" in harness.last_text


async def test_edit_from_confirmation_returns_to_confirmation(harness: BotHarness) -> None:
    await run_to_confirm(harness)
    await harness.press(texts.BTN_EDIT)
    await harness.press(texts.BTN_EDIT_SORT)
    await harness.press(texts.SORT_LABELS[SortKey.PROBABILITY])
    assert "So'rov" in harness.last_text
    assert "Ehtimollik bo'yicha" in harness.last_text


async def test_save_as_template(harness: BotHarness) -> None:
    await run_to_confirm(harness)
    await harness.press(texts.BTN_SAVE_TEMPLATE)
    methods = await harness.send("Bugun · 50%+")

    assert "Shablon saqlandi" in sent_texts(methods)[-1]
    templates = await harness.services.templates.list_templates(1001)
    assert [template.name for template in templates] == ["Bugun · 50%+"]


async def test_double_start_tap_is_reported_as_running(harness: BotHarness) -> None:
    await run_to_confirm(harness)
    message_id, data = harness.find_button(texts.BTN_START)
    await harness.press_data(data, message_id)

    again = await harness.press_data(data, message_id)

    assert answers(again)[0].text in {texts.STALE_BUTTON, texts.ALREADY_RUNNING}


async def test_button_after_restart_is_stale(harness: BotHarness) -> None:
    await harness.send(texts.MENU_ANALYSIS)
    harness.restart()

    methods = await harness.press(texts.BTN_TODAY)

    assert len(answers(methods)) == 1
    assert answers(methods)[0].text == texts.STALE_BUTTON
    assert answers(methods)[0].show_alert is True


async def test_old_wizard_message_buttons_are_stale(harness: BotHarness) -> None:
    await harness.send(texts.MENU_ANALYSIS)
    old_id, data = harness.find_button(texts.BTN_TODAY)
    await harness.send(texts.MENU_ANALYSIS)  # a new wizard message replaces the old one

    methods = await harness.press_data(data, old_id)

    assert answers(methods)[0].text == texts.STALE_BUTTON


async def test_budget_exceeded_keeps_user_on_confirmation(harness: BotHarness) -> None:
    await run_to_confirm(harness)
    analyze = AsyncMock(side_effect=BudgetExceededError(needed=45, available=30))
    harness.services.analysis.analyze = analyze  # type: ignore[method-assign]

    methods = await harness.press(texts.BTN_START)

    shown = sent_texts(methods)
    assert any("45 ta so'rov kerak" in text for text in shown)
    assert any("So'rov" in text and "Taxminiy" in text for text in shown)
    assert len(answers(methods)) == 1


async def test_over_budget_estimate_hides_start(harness: BotHarness) -> None:
    estimate = CostEstimate(
        window=DateWindow(TODAY, TODAY), fixtures_found=5, api_requests=45, budget_remaining=30
    )
    harness.services.analysis.estimate_cost = AsyncMock(return_value=estimate)  # type: ignore[method-assign]

    await harness.send("/tahlil bugun")

    assert "API limitidan oshadi" in harness.last_text
    with pytest.raises(AssertionError):
        harness.find_button(texts.BTN_START)
    harness.find_button(texts.BTN_EDIT)


async def test_menu_text_is_not_taken_as_typed_input(harness: BotHarness) -> None:
    await harness.send(texts.MENU_ANALYSIS)
    methods = await harness.send(texts.MENU_REPORT)
    assert "Hisobot" in sent_texts(methods)[0]


async def test_text_on_a_button_only_step_asks_for_buttons(harness: BotHarness) -> None:
    await harness.send(texts.MENU_ANALYSIS)
    await harness.press(texts.BTN_TODAY)
    methods = await harness.send("futbol")
    assert sent_texts(methods) == [texts.USE_BUTTONS]


async def test_progress_message_is_edited(harness: BotHarness) -> None:
    await run_to_confirm(harness)
    methods = await harness.press(texts.BTN_START)
    progress_id = next(
        index
        for index, method in enumerate(harness.session.requests, start=1)
        if isinstance(method, SendMessage) and method.text.startswith("⏳")
    )
    edits = [m for m in methods if isinstance(m, EditMessageText) and m.message_id == progress_id]
    assert edits
    assert edits[-1].text.startswith("✅")


async def test_no_price_analysis_is_followed_by_the_most_likely_picks(harness: BotHarness) -> None:
    window = DateWindow(TODAY, TODAY)
    no_price = AnalysisResult(
        query_id="q1",
        window=window,
        signals=(),
        fixtures_analyzed=4,
        matched=0,
        requested=5,
        api_requests_used=0,
        skipped_no_price=4,
    )
    harness.services.analysis.analyze = AsyncMock(return_value=no_price)  # type: ignore[method-assign]
    await run_to_confirm(harness)

    methods = await harness.press(texts.BTN_START)

    shown = sent_texts(methods)
    assert any(texts.RESULTS_NO_PRICE.format(count=4) in text for text in shown)
    picks = [text for text in shown if text.startswith(texts.TOP_PICKS_AFTER_ANALYSIS)]
    assert len(picks) == 1
    assert "🎯 <b>Eng ehtimolli tanlovlar" in picks[0]


async def test_failed_picks_after_analysis_only_say_so(harness: BotHarness) -> None:
    no_price = AnalysisResult(
        query_id="q2",
        window=DateWindow(TODAY, TODAY),
        signals=(),
        fixtures_analyzed=1,
        matched=0,
        requested=5,
        api_requests_used=0,
        skipped_no_price=1,
    )
    harness.services.analysis.analyze = AsyncMock(return_value=no_price)  # type: ignore[method-assign]
    harness.services.matches.top_picks = AsyncMock(  # type: ignore[method-assign]
        side_effect=ServiceUnavailableError("football-data", "HTTP 503")
    )
    await run_to_confirm(harness)

    shown = sent_texts(await harness.press(texts.BTN_START))

    assert any(
        text.startswith("ℹ️ Koeffitsientsiz tanlovlarni hozir olib bo'lmadi") for text in shown
    )
