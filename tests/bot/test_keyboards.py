"""Tests for app.bot.keyboards: layouts and the 64-byte ``callback_data`` limit."""

from datetime import date, timedelta

import pytest
from aiogram.types import InlineKeyboardMarkup
from hypothesis import given
from hypothesis import strategies as st

from app.bot import keyboards, texts
from app.bot.callbacks import CalendarCb, RecalcAllCb, decode_ref
from app.bot.keyboards import PickerRow
from app.bot.states import RESULTS_TOKEN_MODULUS
from app.domain import MarketType, Sport
from app.services.interfaces import (
    MAX_RETRO_LOOKBACK_DAYS,
    MAX_WINDOW_DAYS,
    RiskProfile,
    ScopeKind,
    SignalSource,
    SignalState,
    SortKey,
    SportOption,
    is_valid_entity_id,
)
from tests.bot.samples import make_signal

TELEGRAM_CALLBACK_LIMIT = 64
TODAY = date(2026, 10, 21)
OPTIONS = tuple(
    SportOption(
        sport=sport,
        available=True,
        experimental=sport is Sport.HOCKEY,
        supported_markets=frozenset(MarketType),
    )
    for sport in Sport
)


def callbacks(markup: InlineKeyboardMarkup) -> list[str]:
    return [b.callback_data or "" for row in markup.inline_keyboard for b in row]


def assert_fits(markup: InlineKeyboardMarkup) -> None:
    for data in callbacks(markup):
        assert data
        assert len(data.encode()) <= TELEGRAM_CALLBACK_LIMIT


def static_keyboards() -> list[InlineKeyboardMarkup]:
    return [
        keyboards.dates_keyboard(),
        keyboards.sports_keyboard(OPTIONS),
        keyboards.probability_keyboard(0.45),
        keyboards.ev_keyboard(0.05),
        *(keyboards.scope_keyboard(kind) for kind in ScopeKind),
        keyboards.count_keyboard(10, per_day=True),
        keyboards.markets_keyboard({MarketType.TOTALS}, set(MarketType)),
        *(keyboards.sort_keyboard(key) for key in SortKey),
        keyboards.confirm_keyboard(can_start=True),
        keyboards.confirm_keyboard(can_start=False),
        keyboards.edit_keyboard(),
        keyboards.settings_keyboard(),
        *(keyboards.risk_keyboard(profile) for profile in RiskProfile),
        *(keyboards.settings_input_keyboard(field) for field in keyboards.SETTINGS_FIELDS),
        keyboards.nav_keyboard(default=True),
    ]


def test_static_keyboards_fit_callback_limit() -> None:
    for markup in static_keyboards():
        assert_fits(markup)


@given(
    signal_id=st.integers(min_value=0, max_value=2**63 - 1),
    page=st.integers(min_value=0, max_value=10**6),
    count=st.integers(min_value=1, max_value=100),
    retro=st.booleans(),
    token=st.integers(min_value=0, max_value=RESULTS_TOKEN_MODULUS - 1),
)
def test_result_keyboards_fit(
    signal_id: int, page: int, count: int, retro: bool, token: int
) -> None:
    assert_fits(keyboards.card_keyboard(signal_id, retro=retro))
    assert_fits(keyboards.more_results_keyboard(page, count, token))


@given(
    labels=st.lists(st.text(min_size=1, max_size=200), min_size=1, max_size=30),
    page=st.integers(min_value=0, max_value=1000),
    page_count=st.integers(min_value=1, max_value=1000),
)
def test_picker_keyboards_fit(labels: list[str], page: int, page_count: int) -> None:
    rows = [
        PickerRow(label=label, index=None if i % 4 == 0 else i, selected=i % 2 == 0)
        for i, label in enumerate(labels)
    ]
    assert_fits(keyboards.picker_keyboard(rows, page, page_count))


@given(
    offset=st.integers(min_value=-120, max_value=60),
    start_offset=st.none() | st.integers(min_value=-90, max_value=13),
)
def test_calendar_keyboards_fit(offset: int, start_offset: int | None) -> None:
    month = keyboards.month_key(TODAY + timedelta(days=offset))
    start = None if start_offset is None else TODAY + timedelta(days=start_offset)
    assert_fits(keyboards.calendar_keyboard(month, TODAY, start))


@given(
    field=st.sampled_from(keyboards.SETTINGS_FIELDS),
    labels=st.lists(st.text(min_size=1, max_size=50), max_size=40),
)
def test_settings_editors_fit(field: str, labels: list[str]) -> None:
    assert_fits(keyboards.settings_choice_keyboard(field, labels, current=0))
    assert_fits(
        keyboards.settings_toggle_keyboard(
            field, labels, [True] * len(labels), [f"v{i}" for i in range(len(labels))]
        )
    )


def test_calendar_only_allows_days_within_horizon() -> None:
    markup = keyboards.calendar_keyboard(keyboards.month_key(TODAY), TODAY, None)
    days = [
        CalendarCb.unpack(data).value for data in callbacks(markup) if data.startswith("wc:day")
    ]
    selectable = {date.fromordinal(value) for value in days}
    assert TODAY in selectable
    assert max(selectable) == date(2026, 10, 31)  # end of month, inside today + 13
    november = keyboards.calendar_keyboard(keyboards.month_key(date(2026, 11, 1)), TODAY, None)
    last = max(
        date.fromordinal(CalendarCb.unpack(d).value)
        for d in callbacks(november)
        if d.startswith("wc:day")
    )
    assert last == TODAY + timedelta(days=13)


def test_calendar_disables_days_before_start_and_marks_start() -> None:
    start = TODAY + timedelta(days=2)
    markup = keyboards.calendar_keyboard(keyboards.month_key(TODAY), TODAY, start)
    texts_ = [b.text for row in markup.inline_keyboard for b in row]
    assert f"[{start.day}]" in texts_
    selectable = [
        date.fromordinal(CalendarCb.unpack(d).value)
        for d in callbacks(markup)
        if d.startswith("wc:day")
    ]
    assert min(selectable) == start


def test_calendar_month_navigation_stops_at_bounds() -> None:
    markup = keyboards.calendar_keyboard(keyboards.month_key(date(2026, 11, 1)), TODAY, None)
    month_moves = [d for d in callbacks(markup) if d.startswith("wc:month")]
    assert month_moves == ["wc:month:202610"]


def test_picker_pagination_buttons() -> None:
    rows = [PickerRow(label="A", index=0, selected=True)]
    first = [b.text for row in keyboards.picker_keyboard(rows, 0, 3).inline_keyboard for b in row]
    assert "1/3" in first
    assert texts.BTN_NEXT_PAGE in first
    assert texts.BTN_PREV_PAGE not in first
    assert f"{texts.CHECKED} A" in first


def test_sports_keyboard_marks_experimental_and_offers_all() -> None:
    labels = [b.text for row in keyboards.sports_keyboard(OPTIONS).inline_keyboard for b in row]
    assert "🏒 Xokkey 🧪" in labels
    assert texts.ALL_SPORTS in labels


def test_markets_keyboard_offers_only_supported() -> None:
    markup = keyboards.markets_keyboard(set(), {MarketType.TOTALS})
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any("Total" in label for label in labels)
    assert not any("1X2" in label for label in labels)


def test_retro_card_has_no_bet_or_recalc() -> None:
    labels = [b.text for row in keyboards.card_keyboard(1, retro=True).inline_keyboard for b in row]
    assert texts.BTN_BET not in labels
    assert texts.BTN_RECALC not in labels
    assert texts.BTN_HIDE in labels


def test_main_menu_texts_are_known() -> None:
    assert texts.MENU_ANALYSIS in keyboards.MENU_TEXTS
    assert len(keyboards.MENU_TEXTS) == 9


def test_calendar_end_is_limited_to_the_longest_window_after_the_start() -> None:
    start = TODAY - timedelta(days=40)  # 11.09
    markup = keyboards.calendar_keyboard(keyboards.month_key(TODAY), TODAY, start)
    selectable = [
        date.fromordinal(CalendarCb.unpack(d).value)
        for d in callbacks(markup)
        if d.startswith("wc:day")
    ]
    assert max(selectable) == start + timedelta(days=MAX_WINDOW_DAYS - 1)  # 11.10


def test_calendar_start_reaches_back_exactly_the_retro_lookback() -> None:
    first, _ = keyboards.calendar_bounds(TODAY)
    assert first == TODAY - timedelta(days=MAX_RETRO_LOOKBACK_DAYS)


# --------------------------------------------------------------------------- F6.7 / bets
@given(query_id=st.text(min_size=1, max_size=40).filter(is_valid_entity_id))
def test_recalc_query_keyboard_fits_and_round_trips_any_entity_id(query_id: str) -> None:
    markup = keyboards.recalc_query_keyboard(query_id)

    assert_fits(markup)
    data = RecalcAllCb.unpack(markup.inline_keyboard[0][0].callback_data or "")
    assert decode_ref(data.ref) == query_id


def test_recalc_day_keyboard_fits() -> None:
    assert_fits(keyboards.recalc_day_keyboard(date(2026, 10, 22)))


@pytest.mark.parametrize("garbage", ["!!!", "%%%%", "ÿ", "", "/w=="])
def test_decode_ref_rejects_garbage(garbage: str) -> None:
    assert decode_ref(garbage) is None


@given(
    signal_id=st.integers(min_value=0, max_value=2**63 - 1),
    can_use_defaults=st.booleans(),
)
def test_bet_keyboards_fit(signal_id: int, can_use_defaults: bool) -> None:
    assert_fits(keyboards.bet_prompt_keyboard(signal_id, "1.85", can_use_defaults=can_use_defaults))
    assert_fits(keyboards.bet_cancel_keyboard(signal_id))


def test_bet_prompt_offers_the_default_only_when_a_stake_is_known() -> None:
    with_default = keyboards.bet_prompt_keyboard(7, "1.85", can_use_defaults=True)
    without = keyboards.bet_prompt_keyboard(7, "1.85", can_use_defaults=False)

    assert with_default.inline_keyboard[0][0].text == "✅ Yozish: 1.85"
    assert not any("Yozish" in b.text for row in without.inline_keyboard for b in row)


def test_retro_and_cancelled_signals_cannot_be_recalculated_or_bet_on() -> None:
    for signal in (
        make_signal(source=SignalSource.RETRO, recommended_at=None),
        make_signal(state=SignalState.CANCELLED),
    ):
        labels = [b.text for row in keyboards.signal_keyboard(signal).inline_keyboard for b in row]
        assert texts.BTN_RECALC not in labels
        assert texts.BTN_BET not in labels
    active = [
        b.text for row in keyboards.signal_keyboard(make_signal()).inline_keyboard for b in row
    ]
    assert texts.BTN_RECALC in active
    assert texts.BTN_BET in active
