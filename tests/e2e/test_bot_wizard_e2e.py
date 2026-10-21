"""QA: the 📊 Tahlil wizard end to end, asserting what the user sees (plan §6.2, F6.4).

Clock: Wednesday 21.10.2026 11:00 Tashkent; ``horizon_end`` = Tuesday 03.11.2026.
"""

from datetime import date, timedelta
from typing import Any

import pytest

from app.bot import texts
from app.bot.callbacks import CalendarCb, PickCb
from app.domain import Sport
from app.services.interfaces import (
    MAX_FUTURE_DAYS,
    MAX_RETRO_LOOKBACK_DAYS,
    MAX_SCOPE_IDS,
    MAX_WINDOW_DAYS,
    AnalysisQuery,
    AnalysisResult,
    DateSpec,
    DateWindow,
    FixtureListRequest,
    ScopeKind,
    Services,
    SortKey,
    horizon_end,
)
from tests.bot.fakes import FIXED_NOW
from tests.e2e.bot_client import Client, answers, shown
from tests.e2e.flows import BACK_TARGET, WIZARD_SCREENS, reach

TODAY = date(2026, 10, 21)


def window(start: date, end: date) -> str:
    if start == end:
        return texts.WINDOW_SINGLE.format(day=start.strftime("%d.%m"))
    return texts.WINDOW_RANGE.format(start=start.strftime("%d.%m"), end=end.strftime("%d.%m"))


class AnalyzeSpy:
    """Wraps the fake ``analyze`` to record queries and results."""

    def __init__(self, services: Services) -> None:
        self.queries: list[AnalysisQuery] = []
        self.results: list[AnalysisResult] = []
        self._real = services.analysis.analyze
        services.analysis.analyze = self  # type: ignore[method-assign]

    async def __call__(self, user_id: int, query: AnalysisQuery, **kwargs: Any) -> AnalysisResult:
        self.queries.append(query)
        result = await self._real(user_id, query, **kwargs)
        self.results.append(result)
        return result


async def finish_from_sport(client: Client, sort: SortKey = SortKey.EV) -> None:
    """From the sport step to the confirmation with plain choices."""
    await client.press(texts.SPORT_LABELS[Sport.FOOTBALL])
    await client.press("50%")
    await client.press("5%")
    await client.press(texts.BTN_SCOPE_ALL)
    await client.press(texts.BTN_ALL)
    await client.press(texts.BTN_DONE)
    await client.press(texts.SORT_LABELS[sort])


def test_clock_assumption() -> None:
    assert FIXED_NOW.date() == TODAY
    assert TODAY.weekday() == 2
    assert horizon_end(TODAY) == date(2026, 11, 3)


# --------------------------------------------------------------------------- date modes
@pytest.mark.parametrize(
    ("button", "start", "end"),
    [
        (texts.BTN_TODAY, TODAY, TODAY),
        (texts.BTN_TOMORROW, TODAY + timedelta(days=1), TODAY + timedelta(days=1)),
        (texts.BTN_WEEKEND, date(2026, 10, 24), date(2026, 10, 25)),
        (texts.BTN_NEXT_DAYS.format(days=3), TODAY, TODAY + timedelta(days=2)),
        (texts.BTN_NEXT_DAYS.format(days=7), TODAY, TODAY + timedelta(days=6)),
    ],
)
async def test_each_quick_date_mode_runs_to_results(
    admin: Client, services: Services, button: str, start: date, end: date
) -> None:
    spy = AnalyzeSpy(services)
    await admin.send(texts.MENU_ANALYSIS)
    await admin.press(button)
    await finish_from_sport(admin)

    assert f"<b>So'rov:</b> {window(start, end)} ·" in admin.last_text
    assert admin.has_button(texts.BTN_START)

    methods = await admin.press(texts.BTN_START)

    assert len(answers(methods)) == 1
    assert spy.queries[0].dates.resolve(TODAY) == DateWindow(start, end)
    texts_shown = shown(methods)
    assert texts_shown[0].endswith(texts.WIZARD_STARTED)
    assert texts_shown[1] == texts.PROGRESS.format(done=0, total="…")
    assert any(t.startswith("✅ Tahlil tugadi") for t in texts_shown)
    summary = next(t for t in texts_shown if "Natijalar" in t or "Value topilmadi" in t)
    assert window(start, end) in summary
    assert await admin.state() is None


async def test_calendar_range_crossing_month_boundary(admin: Client, services: Services) -> None:
    spy = AnalyzeSpy(services)
    await admin.send(texts.MENU_ANALYSIS)
    await admin.press(texts.BTN_CALENDAR)
    assert admin.last_text == texts.CALENDAR_PICK_START
    october = admin.last_message_id

    start, end = date(2026, 10, 30), date(2026, 11, 2)
    await admin.press_data(CalendarCb(action="day", value=start.toordinal()).pack(), october)
    assert admin.last_text == texts.CALENDAR_PICK_END.format(start="30.10")
    assert admin.has_button("[30]")

    await admin.press(texts.BTN_NEXT_PAGE)
    assert admin.has_button(f"{texts.MONTHS[10]} 2026")
    # In November only 1..3 are selectable (horizon 03.11); later days are inert.
    labels = [label for label, _ in admin.live_buttons(admin.last_message_id)]
    assert {"1", "2", "3"} <= set(labels)
    assert "4" not in labels
    assert texts.BTN_NEXT_PAGE not in labels  # no December: it is beyond the horizon

    november = admin.last_message_id
    assert november == october  # the same wizard message is edited
    datas = [data for _, data in admin.live_buttons(november)]
    assert CalendarCb(action="day", value=end.toordinal()).pack() in datas
    await admin.press_data(CalendarCb(action="day", value=end.toordinal()).pack(), november)
    assert "2/7 · Sport turi" in admin.last_text
    await finish_from_sport(admin)
    assert f"{window(start, end)} ·" in admin.last_text

    await admin.press(texts.BTN_START)
    assert spy.queries[0].dates.resolve(TODAY) == DateWindow(start, end)


async def test_calendar_back_and_previous_month_keep_the_start(admin: Client) -> None:
    await admin.send(texts.MENU_ANALYSIS)
    await admin.press(texts.BTN_CALENDAR)
    message_id = admin.last_message_id
    await admin.press_data(CalendarCb(action="day", value=TODAY.toordinal()).pack(), message_id)
    await admin.press(texts.BTN_NEXT_PAGE)
    await admin.press(texts.BTN_PREV_PAGE)

    assert admin.last_text == texts.CALENDAR_PICK_END.format(start="21.10")
    await admin.press(texts.BTN_BACK)
    assert await admin.state() == "AnalysisFlow:dates"


async def test_calendar_same_day_twice_is_a_one_day_window(admin: Client) -> None:
    await admin.send(texts.MENU_ANALYSIS)
    await admin.press(texts.BTN_CALENDAR)
    data = CalendarCb(action="day", value=(TODAY + timedelta(days=5)).toordinal()).pack()
    await admin.press_data(data, admin.last_message_id)
    await admin.press_data(data, admin.last_message_id)
    await finish_from_sport(admin)
    assert f"<b>So'rov:</b> {window(TODAY + timedelta(days=5), TODAY + timedelta(days=5))} ·" in (
        admin.last_text
    )


# --------------------------------------------------------------------------- scopes
async def _to_scope(client: Client, date_button: str = texts.BTN_TODAY) -> None:
    await client.send(texts.MENU_ANALYSIS)
    await client.press(date_button)
    await client.press(texts.SPORT_LABELS[Sport.FOOTBALL])
    await client.press("50%")
    await client.press("5%")


async def _finish_from_count(client: Client) -> None:
    await client.press(texts.BTN_ALL)
    await client.press(texts.BTN_DONE)
    await client.press(texts.SORT_LABELS[SortKey.KICKOFF])


async def test_leagues_scope_end_to_end(admin: Client, services: Services) -> None:
    spy = AnalyzeSpy(services)
    await _to_scope(admin, texts.BTN_NEXT_DAYS.format(days=3))
    await admin.press(texts.BTN_SCOPE_LEAGUES)
    await admin.press("Premier League")
    await admin.press("La Liga")
    await admin.press("Premier League")  # untoggle
    await admin.press("Serie A")
    assert texts.WIZARD_LEAGUES.format(count=2) in admin.last_text
    await admin.press(texts.BTN_DONE)
    await _finish_from_count(admin)

    assert "<b>Qamrov:</b> La Liga, Serie A ·" in admin.last_text
    await admin.press(texts.BTN_START)
    assert spy.queries[0].scope is ScopeKind.LEAGUES
    assert spy.queries[0].scope_ids == {"af:l140", "af:l135"}
    leagues = {signal.league for signal in spy.results[0].signals}
    assert leagues <= {"La Liga", "Serie A"}


async def test_teams_scope_end_to_end(admin: Client, services: Services) -> None:
    spy = AnalyzeSpy(services)
    await _to_scope(admin, texts.BTN_NEXT_DAYS.format(days=7))
    await admin.press(texts.BTN_SCOPE_TEAMS)
    await admin.send("Arsenal, Inter")
    assert texts.WIZARD_TEAMS.format(count=2) in admin.last_text
    await admin.press(texts.BTN_DONE)
    await _finish_from_count(admin)

    assert "<b>Qamrov:</b> Arsenal, Inter ·" in admin.last_text
    await admin.press(texts.BTN_START)
    query = spy.queries[0]
    assert query.scope is ScopeKind.TEAMS
    assert len(query.scope_ids) == 2
    for signal in spy.results[0].signals:
        assert {"Arsenal", "Inter"} & {signal.home_team, signal.away_team}


async def test_fixtures_scope_selection_survives_paging(admin: Client, services: Services) -> None:
    spy = AnalyzeSpy(services)
    await _to_scope(admin, texts.BTN_NEXT_DAYS.format(days=7))
    await admin.press(texts.BTN_SCOPE_FIXTURES)
    picker = admin.last_message_id
    assert "Sahifa 1/" in admin.last_text

    football = frozenset({Sport.FOOTBALL})
    pages = [
        await services.catalog.list_fixtures(
            admin.user_id,
            FixtureListRequest(
                dates=DateSpec.next_days(7), sports=football, page=page, page_size=8
            ),
        )
        for page in (0, 1)
    ]
    first_page, second_page = pages
    assert first_page.page_count >= 2

    await admin.press_data(PickCb(action="toggle", index=0).pack(), picker)
    await admin.press(texts.BTN_NEXT_PAGE)
    assert "Sahifa 2/" in admin.last_text
    assert texts.WIZARD_FIXTURES.split("(")[0] in admin.last_text
    await admin.press_data(PickCb(action="toggle", index=0).pack(), picker)
    await admin.press(texts.BTN_PREV_PAGE)
    assert "Sahifa 1/" in admin.last_text
    assert "(tanlangan: 2)" in admin.last_text
    # The page-1 pick is still ticked after coming back.
    first_label = next(
        label
        for label, data in admin.live_buttons(picker)
        if data == PickCb(action="toggle").pack()
    )
    assert first_label.startswith(texts.CHECKED)

    await admin.press(texts.BTN_DONE)
    await _finish_from_count(admin)
    for fixture in (first_page.items[0], second_page.items[0]):
        assert fixture.label.split(" — ")[0] in admin.last_text or fixture.label in admin.last_text

    await admin.press(texts.BTN_START)
    assert spy.queries[0].scope is ScopeKind.FIXTURES
    assert spy.queries[0].scope_ids == {
        first_page.items[0].fixture_id,
        second_page.items[0].fixture_id,
    }


# --------------------------------------------------------------------------- invalid input
@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("31.02", texts.PARSE_BAD_DATE.format(value="31.02")),
        ("salom", texts.PARSE_BAD_RANGE.format(value="salom")),
        ("21.10 - 04.11", texts.PARSE_TOO_FAR.format(max_future=MAX_FUTURE_DAYS)),
        ("01.08 - 15.09", texts.PARSE_WINDOW_TOO_LONG.format(max_window=MAX_WINDOW_DAYS)),
        ("01.12.2026", texts.PARSE_TOO_FAR.format(max_future=MAX_FUTURE_DAYS)),
        (
            "01.01.2026 - 05.01.2026",
            texts.PARSE_TOO_OLD.format(max_lookback=MAX_RETRO_LOOKBACK_DAYS),
        ),
    ],
)
async def test_invalid_typed_dates_keep_the_step(admin: Client, typed: str, expected: str) -> None:
    await admin.send(texts.MENU_ANALYSIS)
    wizard_message = admin.last_message_id

    methods = await admin.send(typed)

    assert shown(methods) == [expected]
    assert await admin.state() == "AnalysisFlow:dates"
    # The wizard message keeps its keyboard and still works.
    assert admin.markups[wizard_message] is not None
    await admin.send("21.10 - 03.11")
    assert "2/7 · Sport turi" in admin.last_text


async def test_retro_range_beyond_the_window_in_calendar_keeps_start(admin: Client) -> None:
    await admin.send(texts.MENU_ANALYSIS)
    await admin.press(texts.BTN_CALENDAR)
    message_id = admin.last_message_id
    far_past = TODAY - timedelta(days=60)
    await admin.press_data(CalendarCb(action="day", value=far_past.toordinal()).pack(), message_id)

    methods = await admin.press_data(
        CalendarCb(action="day", value=TODAY.toordinal()).pack(), message_id
    )

    alert = answers(methods)
    assert len(alert) == 1
    assert alert[0].show_alert is True
    assert alert[0].text == texts.PARSE_WINDOW_TOO_LONG.format(max_window=MAX_WINDOW_DAYS)
    assert await admin.state() == "AnalysisFlow:calendar"
    # The start is kept: a valid end now finishes the step.
    end = far_past + timedelta(days=MAX_WINDOW_DAYS - 1)
    await admin.press_data(CalendarCb(action="day", value=end.toordinal()).pack(), message_id)
    assert "2/7 · Sport turi" in admin.last_text


async def test_retro_range_within_the_window_is_marked_retro(
    admin: Client, services: Services
) -> None:
    spy = AnalyzeSpy(services)
    await admin.send(texts.MENU_ANALYSIS)
    await admin.send("14.10 - 20.10")
    await finish_from_sport(admin)

    assert texts.WIZARD_CONFIRM_RETRO in admin.last_text
    methods = await admin.press(texts.BTN_START)

    assert any(texts.RESULTS_RETRO in t for t in shown(methods))
    cards = [t for t in shown(methods) if t.startswith("⚽")]
    assert cards
    for card in cards:
        assert texts.CARD_RETRO in card
    on_first_page = {
        signal.fixture_label: signal
        for signal in spy.results[0].signals
        if any(signal.fixture_label in card for card in cards)
    }
    assert len(on_first_page) == len(cards)
    checked = 0
    for card in cards:
        signal = next(s for label, s in on_first_page.items() if label in card)
        if signal.result is not None:
            assert texts.CARD_RESULT.format(result=texts.RESULT_LABELS[signal.result]) in card
            checked += 1
    assert checked
    retro_keyboards = [
        [label for label, _ in admin.live_buttons(message_id)]
        for message_id, text in admin.texts.items()
        if text.startswith("⚽")
    ]
    for labels in retro_keyboards:
        assert texts.BTN_BET not in labels
        assert texts.BTN_RECALC not in labels


@pytest.mark.parametrize(
    ("custom_button_step", "typed", "expected", "state"),
    [
        (
            "probability",
            "150%",
            texts.PARSE_PERCENT_RANGE.format(value="150%", low="30", high="95"),
            "AnalysisFlow:probability_input",
        ),
        (
            "probability",
            "29",
            texts.PARSE_PERCENT_RANGE.format(value="29", low="30", high="95"),
            "AnalysisFlow:probability_input",
        ),
        (
            "ev",
            "-3",
            texts.PARSE_PERCENT_RANGE.format(value="-3", low="0", high="20"),
            "AnalysisFlow:ev_input",
        ),
        (
            "ev",
            "21%",
            texts.PARSE_PERCENT_RANGE.format(value="21%", low="0", high="20"),
            "AnalysisFlow:ev_input",
        ),
        (
            "ev",
            "ko'p",
            texts.PARSE_NOT_NUMBER.format(value="ko'p", example="60"),
            "AnalysisFlow:ev_input",
        ),
    ],
)
async def test_invalid_custom_numbers_keep_the_step(
    admin: Client, custom_button_step: str, typed: str, expected: str, state: str
) -> None:
    await admin.send(texts.MENU_ANALYSIS)
    await admin.press(texts.BTN_TODAY)
    await admin.press(texts.SPORT_LABELS[Sport.FOOTBALL])
    if custom_button_step == "ev":
        await admin.press("60%")
    await admin.press(texts.BTN_CUSTOM)

    methods = await admin.send(typed)

    assert shown(methods) == [expected]
    assert await admin.state() == state
    valid, next_screen = (
        ("65", "3/7 · Minimal EV")
        if custom_button_step == "probability"
        else (
            "12",
            "4/7 · Qamrov",
        )
    )
    after = await admin.send(valid)
    assert next_screen in shown(after)[-1]


async def test_count_out_of_range_keeps_step(admin: Client) -> None:
    await _to_scope(admin)
    await admin.press(texts.BTN_SCOPE_ALL)
    await admin.press(texts.BTN_CUSTOM)

    methods = await admin.send("101")

    assert shown(methods) == [texts.PARSE_COUNT_RANGE.format(value="101", low=1, high=100)]
    assert await admin.state() == "AnalysisFlow:count_input"


# --------------------------------------------------------------------------- back / cancel
@pytest.mark.parametrize("screen", WIZARD_SCREENS)
async def test_every_step_has_working_back_and_cancel(admin: Client, screen: str) -> None:
    await reach(admin, screen)
    wizard_message = admin.last_message_id
    labels = [label for label, _ in admin.live_buttons(wizard_message)]
    assert texts.BTN_BACK in labels
    assert texts.BTN_CANCEL in labels

    back = await admin.press_data(*_button(admin, wizard_message, texts.BTN_BACK))
    assert len(answers(back)) == 1
    target = BACK_TARGET[screen]
    assert await admin.state() == (None if target is None else f"AnalysisFlow:{target}")

    await reach(admin, screen)
    wizard_message = admin.last_message_id
    cancel = await admin.press_data(*_button(admin, wizard_message, texts.BTN_CANCEL))
    assert len(answers(cancel)) == 1
    assert shown(cancel) == [texts.CANCELLED]
    assert await admin.state() is None
    assert admin.markups[wizard_message] is None


def _button(client: Client, message_id: int, label: str) -> tuple[str, int]:
    data = next(data for text, data in client.live_buttons(message_id) if text == label)
    return data, message_id


async def test_scope_selection_is_capped_at_max_scope_ids(admin: Client) -> None:
    await admin.send(texts.MENU_ANALYSIS)
    await admin.send("21.10 - 03.11")  # the whole 14-day horizon: > 50 fixtures
    for label in (texts.SPORT_LABELS[Sport.FOOTBALL], "50%", "5%", texts.BTN_SCOPE_FIXTURES):
        await admin.press(label)
    picker = admin.last_message_id
    picked = 0
    while picked < MAX_SCOPE_IDS:
        page_items = sum(
            1 for _, data in admin.live_buttons(picker) if data.startswith("wk:toggle")
        )
        for index in range(min(page_items, MAX_SCOPE_IDS - picked)):
            await admin.press_data(PickCb(action="toggle", index=index).pack(), picker)
            picked += 1
        if picked < MAX_SCOPE_IDS:
            await admin.press(texts.BTN_NEXT_PAGE)
    assert f"(tanlangan: {MAX_SCOPE_IDS})" in admin.last_text
    extra = next(
        data
        for label, data in admin.live_buttons(picker)
        if data.startswith("wk:toggle") and not label.startswith(texts.CHECKED)
    )

    methods = await admin.press_data(extra, picker)

    assert answers(methods)[0].text == texts.SCOPE_LIMIT.format(max_scope=MAX_SCOPE_IDS)
    assert answers(methods)[0].show_alert is True
    assert f"(tanlangan: {MAX_SCOPE_IDS})" in admin.last_text
