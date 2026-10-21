"""Tests for app.bot.parsing (pure parsers, including property-based ones)."""

from datetime import date, time, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.bot.parsing import (
    MAX_QUICK_LENGTH,
    InputError,
    check_window,
    parse_count,
    parse_date,
    parse_date_range,
    parse_money,
    parse_number,
    parse_odds_range,
    parse_percent,
    parse_quick_query,
    parse_time,
    parse_time_range,
    parse_timezone,
)
from app.domain import Sport
from app.services.interfaces import DateMode, DateSpec

TODAY = date(2026, 10, 21)
BOUNDS = {"probability_bounds": (0.30, 0.95), "ev_bounds": (0.0, 0.20)}


def quick(text: str):  # type: ignore[no-untyped-def]
    return parse_quick_query(text, TODAY, **BOUNDS)


# --------------------------------------------------------------------------- numbers
@pytest.mark.parametrize(("text", "expected"), [("60", 60.0), ("62,5", 62.5), (" 7% ", 7.0)])
def test_parse_number(text: str, expected: float) -> None:
    assert parse_number(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "nan", "inf", "1e999", "%"])
def test_parse_number_rejects_garbage(text: str) -> None:
    with pytest.raises(InputError):
        parse_number(text)


def test_parse_percent_bounds_and_message() -> None:
    assert parse_percent("65", 0.30, 0.95) == 0.65
    with pytest.raises(InputError, match="30–95%"):
        parse_percent("150%", 0.30, 0.95)
    with pytest.raises(InputError):
        parse_percent("29.9", 0.30, 0.95)


def test_parse_count() -> None:
    assert parse_count("12", 1, 100) == 12
    for bad in ("0", "101", "2.5", "-3"):
        with pytest.raises(InputError):
            parse_count(bad, 1, 100)


def test_parse_money() -> None:
    assert parse_money("1 000 000") == Decimal(1000000)
    assert parse_money("12_500,50") == Decimal("12500.50")
    for bad in ("0", "-5", "abc", "NaN", "Infinity"):
        with pytest.raises(InputError):
            parse_money(bad)


def test_parse_odds_range() -> None:
    assert parse_odds_range("1.40-3.00") == (1.40, 3.00)
    assert parse_odds_range("1,5 – 2,5") == (1.5, 2.5)
    for bad in ("3.00-1.40", "1.00-2.00", "1.5", "a-b", "1.5-2-3", "1.5-200"):
        with pytest.raises(InputError):
            parse_odds_range(bad)


# --------------------------------------------------------------------------- time
def test_parse_time_and_ranges() -> None:
    assert parse_time("9:05") == time(9, 5)
    assert parse_time("08.30") == time(8, 30)
    assert parse_time_range("23:00-07:00") == (time(23), time(7))
    for bad in ("24:00", "9", "12:60"):
        with pytest.raises(InputError):
            parse_time(bad)
    for bad in ("08:00-08:00", "8-9", "08:00"):
        with pytest.raises(InputError):
            parse_time_range(bad)


def test_parse_timezone() -> None:
    assert parse_timezone(" Europe/Moscow ") == "Europe/Moscow"
    with pytest.raises(InputError, match="noma'lum"):
        parse_timezone("Mars/Olympus")
    with pytest.raises(InputError):
        parse_timezone("../etc")


# --------------------------------------------------------------------------- dates
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("25.10", date(2026, 10, 25)),
        ("25/10", date(2026, 10, 25)),
        ("25.10.2026", date(2026, 10, 25)),
        ("25.10.26", date(2026, 10, 25)),
        ("05.01", date(2027, 1, 5)),  # closest to today: next January
        ("20.06", date(2026, 6, 20)),  # closest to today: last June
    ],
)
def test_parse_date(text: str, expected: date) -> None:
    assert parse_date(text, TODAY) == expected


@pytest.mark.parametrize("text", ["31.02", "00.10", "12.13", "29.02.2027", "1.1.0000", "x"])
def test_parse_date_rejects_impossible_dates(text: str) -> None:
    with pytest.raises(InputError):
        parse_date(text, TODAY)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("25.10 - 31.10", (date(2026, 10, 25), date(2026, 10, 31))),
        ("25.10–31.10", (date(2026, 10, 25), date(2026, 10, 31))),
        ("25.10.2026-31.10.2026", (date(2026, 10, 25), date(2026, 10, 31))),
        ("28.12-03.01", (date(2026, 12, 28), date(2027, 1, 3))),
        ("22.10", (date(2026, 10, 22), date(2026, 10, 22))),
    ],
)
def test_parse_date_range(text: str, expected: tuple[date, date]) -> None:
    assert parse_date_range(text, TODAY) == expected


def test_parse_date_range_rejects_text() -> None:
    with pytest.raises(InputError, match="tushunmadim"):
        parse_date_range("next week", TODAY)


def test_check_window_rules() -> None:
    assert check_window(TODAY, TODAY + timedelta(days=14), TODAY).mode is DateMode.RANGE
    retro = check_window(TODAY - timedelta(days=10), TODAY - timedelta(days=5), TODAY)
    assert retro.date_to == TODAY - timedelta(days=5)
    with pytest.raises(InputError, match="oldin"):
        check_window(TODAY, TODAY - timedelta(days=1), TODAY)
    with pytest.raises(InputError, match="uzun"):
        check_window(TODAY - timedelta(days=40), TODAY, TODAY)
    with pytest.raises(InputError, match="uzoqda"):
        check_window(TODAY, TODAY + timedelta(days=15), TODAY)


# --------------------------------------------------------------------------- /tahlil
def test_quick_full_example_any_order() -> None:
    expected = quick("25.10-31.10 futbol 60% Arsenal,Inter")
    shuffled = quick("Arsenal, Inter 60% futbol 25.10-31.10")

    assert expected == shuffled
    assert expected.dates == DateSpec.between(date(2026, 10, 25), date(2026, 10, 31))
    assert expected.sports == {Sport.FOOTBALL}
    assert expected.min_probability == 0.60
    assert expected.team_names == ("Arsenal", "Inter")


def test_quick_missing_parts_are_none() -> None:
    result = quick("")
    assert result.dates is None
    assert result.sports is None
    assert result.min_probability is None
    assert result.team_names == ()


@pytest.mark.parametrize(
    ("text", "spec"),
    [
        ("bugun", DateSpec.today()),
        ("ERTAGA", DateSpec.tomorrow()),
        ("dam olish kunlari", DateSpec.weekend()),
        ("dam", DateSpec.weekend()),
        ("3kun", DateSpec.next_days(3)),
        ("7 kun", DateSpec.next_days(7)),
        ("22.10", DateSpec.between(date(2026, 10, 22), date(2026, 10, 22))),
    ],
)
def test_quick_date_keywords(text: str, spec: DateSpec) -> None:
    assert quick(text).dates == spec


@pytest.mark.parametrize(
    ("text", "sports"),
    [
        ("xokkey", {Sport.HOCKEY}),
        ("hockey futbol", {Sport.HOCKEY, Sport.FOOTBALL}),
        ("футбол", {Sport.FOOTBALL}),
        ("hammasi", set(Sport)),
    ],
)
def test_quick_sport_aliases(text: str, sports: set[Sport]) -> None:
    assert quick(text).sports == sports


def test_quick_ev_count_and_percent() -> None:
    result = quick("ev5% 10ta 65%")
    assert (result.min_ev, result.max_signals, result.min_probability) == (0.05, 10, 0.65)
    assert quick("ev >= 7").min_ev == 0.07


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("60", "foizmi yoki sonmi"),
        ("60% 70%", "ikki marta"),
        ("bugun 22.10", "ikki marta"),
        ("ev5% ev6%", "ikki marta"),
        ("5ta 6ta", "ikki marta"),
        ("150%", "30–95%"),
        ("ev30%", "0–20%"),
        ("31.02", "bunday sana yo'q"),
        ("01.12-05.12", "uzoqda"),
        ("0ta", "1–100"),
        ("20kun", "1–14"),
    ],
)
def test_quick_errors(text: str, message: str) -> None:
    with pytest.raises(InputError, match=message):
        quick(text)


def test_quick_team_names_are_deduplicated_and_normalised() -> None:
    assert quick("  Real   Madrid , real madrid,, Inter ").team_names == ("Real Madrid", "Inter")


def test_quick_too_many_teams_and_too_long() -> None:
    with pytest.raises(InputError, match="ko'p"):
        quick(",".join(f"Team{chr(65 + i % 26)}{chr(65 + i // 26)}" for i in range(51)))
    with pytest.raises(InputError, match="uzun"):
        quick("a" * (MAX_QUICK_LENGTH + 1))


def test_quick_escapes_echoed_input() -> None:
    with pytest.raises(InputError) as caught:
        quick("31.02 <b>")
    assert "<b>" not in caught.value.message.replace("<code>", "").replace("</code>", "")


@given(st.text(max_size=MAX_QUICK_LENGTH + 20))
def test_quick_never_crashes_on_arbitrary_text(text: str) -> None:
    try:
        result = quick(text)
    except InputError as error:
        assert error.message
    else:
        assert all(name.strip() == name and name for name in result.team_names)


@given(
    st.lists(
        st.sampled_from(
            ["bugun", "futbol", "60%", "ev5%", "10ta", "25.10-31.10", "Arsenal", "3kun", ","]
        ),
        max_size=8,
    )
)
def test_quick_never_crashes_on_token_soup(tokens: list[str]) -> None:
    try:
        quick(" ".join(tokens))
    except InputError as error:
        assert error.message


@given(st.text(max_size=40))
def test_date_range_parser_never_crashes(text: str) -> None:
    try:
        start, end = parse_date_range(text, TODAY)
    except InputError:
        return
    assert isinstance(start, date)
    assert isinstance(end, date)
