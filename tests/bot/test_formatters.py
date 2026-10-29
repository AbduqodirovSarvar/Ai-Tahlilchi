"""Snapshot-style tests of app.bot.formatters (cards, grouping, summaries, splitting)."""

import re
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.bot import texts
from app.bot.formatters import (
    MAX_NOTE_CHARS,
    TELEGRAM_TEXT_LIMIT,
    budget_message,
    cooldown_message,
    day_header,
    format_hidden_card,
    format_results_summary,
    format_settings,
    format_signal_card,
    group_by_day,
    money,
    ordered_for_display,
    result_page,
    selection_label,
    split_message,
    sports_label,
    threshold_percent,
    validation_message,
    window_label,
)
from app.domain import Market, MarketType, Selection, Sport
from app.services.errors import BudgetExceededError, CooldownActiveError, ValidationFailedError
from app.services.interfaces import (
    AnalysisResult,
    ConfidenceLevel,
    DateWindow,
    QuietHours,
    ResultMark,
    SignalSource,
    SignalState,
    SignalView,
    UserSettingsView,
    Verdict,
)

TASHKENT = ZoneInfo("Asia/Tashkent")
KICKOFF = datetime(2026, 10, 24, 15, 0, tzinfo=UTC)  # Saturday 20:00 in Tashkent


def signal(**overrides: object) -> SignalView:
    values: dict[str, object] = {
        "signal_id": 1,
        "fixture_id": "f1",
        "sport": Sport.FOOTBALL,
        "league": "Premier League",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "kickoff": KICKOFF,
        "market": Market.totals(2.5),
        "selection": Selection.OVER,
        "odds": 1.85,
        "bookmaker": "bet365",
        "probability": 0.61,
        "ev": 0.129,
        "confidence": ConfidenceLevel.HIGH,
        "stake_units": Decimal("1.5"),
        "llm_note": "Ikki jamoaning so'nggi 5 o'yinidagi xG o'rtachasi 3.1.",
    }
    values.update(overrides)
    return SignalView(**values)  # type: ignore[arg-type]


def test_card_matches_plan_layout() -> None:
    assert format_signal_card(signal(), TASHKENT) == (
        "⚽ <b>Arsenal — Chelsea</b>   🕒 24.10.2026 20:00  🏆 Premier League\n"
        "🎯 Tanlov: <b>Total 2.5 dan ko'p</b>   @ <b>1.85</b>\n"
        "📊 Ehtimollik: 61% (adolatli koeff. 1.64)\n"
        "💹 EV: +12.9%   🛡 Ishonch: Yuqori\n"
        "💰 Tavsiya: 1.5 unit\n"
        "📝 Ikki jamoaning so'nggi 5 o'yinidagi xG o'rtachasi 3.1."
    )


def test_card_escapes_untrusted_text() -> None:
    card = format_signal_card(
        signal(
            home_team="A&B <United>",
            league="<script>",
            llm_note="x < y & z",
            risk_flags=("<rotatsiya>",),
        ),
        TASHKENT,
    )
    assert "A&amp;B &lt;United&gt;" in card
    assert "&lt;script&gt;" in card
    assert "x &lt; y &amp; z" in card
    assert "&lt;rotatsiya&gt;" in card
    assert "<script>" not in card


def test_card_flags() -> None:
    card = format_signal_card(
        signal(
            ev=-0.03,
            preliminary=True,
            watchlist=True,
            experimental=True,
            llm_verdict=Verdict.CAUTION,
            stake_units=Decimal(0),
            data_as_of=datetime(2026, 10, 24, 4, 0, tzinfo=UTC),
            state=SignalState.CANCELLED,
            result=ResultMark.WON,
        ),
        TASHKENT,
    )
    for line in (
        texts.CARD_PRELIMINARY,
        texts.CARD_NEGATIVE_EV,
        texts.CARD_WATCHLIST,
        texts.CARD_EXPERIMENTAL,
        texts.CARD_CAUTION,
        texts.CARD_NO_STAKE,
        texts.CARD_CANCELLED,
        "📦 Ma'lumot 09:00 holatiga",
        "Natija: ✅ O'tdi",
        "EV: -3.0%",
    ):
        assert line in card


def test_no_value_card_and_retro_card() -> None:
    no_value = format_signal_card(signal(no_value=True, ev=0.01), TASHKENT)
    assert texts.CARD_NO_VALUE in no_value
    assert texts.CARD_NEGATIVE_EV not in no_value

    retro = format_signal_card(signal(source=SignalSource.RETRO, result=ResultMark.LOST), TASHKENT)
    assert texts.CARD_RETRO in retro
    assert "❌ O'tmadi" in retro


def test_empty_and_long_notes() -> None:
    assert "📝" not in format_signal_card(signal(llm_note="   "), TASHKENT)
    assert "📝" not in format_signal_card(signal(llm_note=None), TASHKENT)
    long_card = format_signal_card(signal(llm_note="a" * 5000), TASHKENT)
    assert len(long_card) < TELEGRAM_TEXT_LIMIT
    assert "a" * (MAX_NOTE_CHARS - 1) + "…" in long_card


@pytest.mark.parametrize(
    ("market", "selection", "expected"),
    [
        (Market.match_result(), Selection.HOME, "Arsenal g'alabasi (1)"),
        (Market.match_result(), Selection.DRAW, "Durang (X)"),
        (Market.match_result(), Selection.AWAY, "Chelsea g'alabasi (2)"),
        (Market.double_chance(), Selection.HOME_OR_DRAW, "Ikki natija: 1X"),
        (Market.totals(2.5), Selection.UNDER, "Total 2.5 dan kam"),
        (Market.btts(), Selection.YES, "Ikkala jamoa gol uradi: Ha"),
        (Market.btts(), Selection.NO, "Ikkala jamoa gol uradi: Yo'q"),
        (Market.asian_handicap(-0.75), Selection.HOME, "Fora Arsenal (-0.75)"),
        (Market.asian_handicap(-0.75), Selection.AWAY, "Fora Chelsea (+0.75)"),
    ],
)
def test_selection_labels(market: Market, selection: Selection, expected: str) -> None:
    assert selection_label(signal(market=market, selection=selection)) == expected


def test_hidden_card() -> None:
    assert format_hidden_card(signal(home_team="<A>")) == "🙈 Yashirildi: &lt;A&gt; — Chelsea"


def test_grouping_uses_display_timezone_and_uzbek_weekdays() -> None:
    late = signal(signal_id=2, kickoff=datetime(2026, 10, 24, 19, 30, tzinfo=UTC))  # Sun 00:30
    early = signal(signal_id=3, kickoff=datetime(2026, 10, 23, 15, 0, tzinfo=UTC))  # Fri
    groups = group_by_day([signal(), late, early], TASHKENT)

    assert [group.day for group in groups] == [
        date(2026, 10, 23),
        date(2026, 10, 24),
        date(2026, 10, 25),
    ]
    assert day_header(groups[1]) == "📅 <b>Shanba, 24.10</b> — 1 ta signal"
    assert day_header(groups[2], continued=True) == "📅 <b>Yakshanba, 25.10</b> (davomi)"
    in_utc = group_by_day([late], ZoneInfo("UTC"))
    assert in_utc[0].day == date(2026, 10, 24)


def test_grouping_keeps_service_order_within_a_day_and_puts_watchlist_last() -> None:
    first = signal(signal_id=1, ev=0.2)
    second = signal(signal_id=2, ev=0.1, kickoff=KICKOFF - timedelta(hours=2))
    watched = signal(signal_id=3, source=SignalSource.WATCHLIST, watchlist=True)
    ordered = ordered_for_display([watched, first, second], TASHKENT)

    assert [s.signal_id for s in ordered] == [1, 2, 3]
    groups = group_by_day([watched, first], TASHKENT)
    assert groups[-1].day is None
    assert day_header(groups[-1]).startswith("⭐")


def test_result_page_marks_continued_days() -> None:
    day_one = [signal(signal_id=i) for i in range(1, 4)]
    page0 = result_page(day_one, TASHKENT, page=0, page_size=2)
    page1 = result_page(day_one, TASHKENT, page=1, page_size=2)

    assert [item.signal is None for item in page0] == [True, False, False]
    assert "3 ta signal" in page0[0].text
    assert "(davomi)" in page1[0].text
    assert result_page(day_one, TASHKENT, page=5, page_size=2) == ()


def _result(signals: tuple[SignalView, ...], **overrides: object) -> AnalysisResult:
    values: dict[str, object] = {
        "query_id": "q1",
        "window": DateWindow(date(2026, 10, 24), date(2026, 10, 31)),
        "signals": signals,
        "fixtures_analyzed": 12,
        "matched": len(signals),
        "requested": None,
        "api_requests_used": 3,
    }
    values.update(overrides)
    return AnalysisResult(**values)  # type: ignore[arg-type]


def test_results_summary_variants() -> None:
    assert format_results_summary(_result((signal(),))) == (
        "📊 <b>Natijalar</b>: 24.10–31.10 · 1 ta signal · 12 ta o'yin"
    )
    shortfall = format_results_summary(_result((signal(),), matched=3, requested=10))
    assert "Shartlarga 3 ta o'yin mos keldi (siz 10 ta so'ragansiz)" in shortfall
    failed = format_results_summary(_result((signal(),), failed_fixtures=2, retro=True))
    assert "2 ta o'yinni" in failed
    assert "Retro" in failed
    empty = format_results_summary(_result(()))
    assert "Value topilmadi" in empty
    assert "12 ta o'yin" in empty
    watched = signal(source=SignalSource.WATCHLIST, watchlist=True, no_value=True)
    only_watched = format_results_summary(_result((watched,), matched=0, requested=5))
    assert "Value topilmadi" in only_watched
    assert "Sevimli" in only_watched
    assert "Shartlarga" not in only_watched


def test_window_label() -> None:
    assert window_label(DateWindow(date(2026, 10, 25), date(2026, 10, 25))) == "25.10"
    assert window_label(DateWindow(date(2026, 10, 25), date(2026, 10, 31))) == "25.10–31.10"


def test_settings_overview_snapshot() -> None:
    settings = UserSettingsView(
        league_ids=frozenset({"af:l39", "zz:unknown"}),
        bank=Decimal(1000000),
        unit=Decimal("10000.5"),
        quiet_hours=QuietHours(time(23), time(7)),
        prematch_minutes=None,
    )
    text = format_settings(settings, {"af:l39": "Premier League"})
    assert "🏆 Ligalar: Premier League, zz:unknown" in text
    assert "💰 Bank: 1 000 000" in text
    assert "🎲 Unit: 10 000.50" in text
    assert "🌙 Tinch soatlar: 23:00–07:00" in text
    assert "⏱ Pre-match yangilanish: o'chiq" in text
    assert "🛑 Kunlik zarar limiti: kiritilmagan" in text
    assert "📈 Koeffitsient diapazoni: 1.50–3.00" in text


def test_sports_label() -> None:
    assert sports_label({Sport.HOCKEY, Sport.FOOTBALL}) == "⚽ Futbol, 🏒 Xokkey"
    assert sports_label(set(Sport)) == texts.ALL_SPORTS
    assert money(None) == texts.NOT_SET


@pytest.mark.parametrize(
    ("retry_after", "minutes"),
    [(timedelta(seconds=1), 1), (timedelta(minutes=7, seconds=10), 8), (timedelta(0), 1)],
)
def test_cooldown_message_rounds_up(retry_after: timedelta, minutes: int) -> None:
    assert cooldown_message(CooldownActiveError(retry_after)) == (
        f"⏳ {minutes} daqiqadan keyin qayta urinib ko'ring."
    )


def test_budget_and_validation_messages() -> None:
    assert "45 ta so'rov kerak" in budget_message(BudgetExceededError(45, 30))
    assert "30–95%" in validation_message(ValidationFailedError("min_probability", "x"))
    assert "14 kun" in validation_message(ValidationFailedError("date_to", "x"))
    assert validation_message(ValidationFailedError("weird", "x")) == texts.VALIDATION_GENERIC


def test_split_message_respects_limit_and_lines() -> None:
    text = "\n".join(f"line {i}" for i in range(2000))
    chunks = split_message(text, limit=100)
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert "\n".join(chunks) == text
    assert split_message("short") == ["short"]
    assert split_message("x" * 250, limit=100) == ["x" * 100, "x" * 100, "x" * 50]


@given(st.text(alphabet=st.characters(codec="utf-8"), max_size=600), st.integers(10, 200))
def test_split_message_never_exceeds_limit(text: str, limit: int) -> None:
    chunks = split_message(text, limit)
    assert all(len(chunk) <= limit for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_card_with_every_flag_fits_one_message() -> None:
    noisy = replace(
        signal(llm_note="x" * 10_000, risk_flags=tuple("r" * 50 for _ in range(10))),
        preliminary=True,
    )
    assert len(format_signal_card(noisy, TASHKENT)) < TELEGRAM_TEXT_LIMIT


def test_market_labels_cover_every_market() -> None:
    assert set(texts.MARKET_LABELS) == set(MarketType)


@pytest.mark.parametrize(
    ("fraction", "shown"),
    [(0.625, "62.5%"), (0.045, "4.5%"), (0.055, "5.5%"), (0.07, "7%"), (0.6, "60%"), (0.0, "0%")],
)
def test_threshold_percent_keeps_what_the_user_typed(fraction: float, shown: str) -> None:
    assert threshold_percent(fraction) == shown


_ENTITY_OR_TAG = re.compile(r"<[^<>]*>|&#?\w{1,10};")


def test_split_message_never_cuts_inside_an_entity() -> None:
    line = ", ".join("A &amp; B &lt;x&gt;" for _ in range(60))
    chunks = split_message(line, limit=100)

    assert len(chunks) > 5
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert "".join(chunks) == line
    for chunk in chunks:
        assert "&" not in _ENTITY_OR_TAG.sub("", chunk)


def test_split_message_closes_and_reopens_tags_at_a_cut() -> None:
    line = "<b>" + "word <i>it</i> " * 30 + "</b> tail"
    chunks = split_message(line, limit=60)

    assert len(chunks) > 3
    assert all(len(chunk) <= 60 for chunk in chunks)
    for chunk in chunks:
        assert chunk.count("<b>") == chunk.count("</b>")
        assert chunk.count("<i>") == chunk.count("</i>")
    plain = re.sub(r"</?[bi]>", "", "".join(chunks))
    assert plain == re.sub(r"</?[bi]>", "", line)


@given(st.text(alphabet="ab &;<>/bi#1", max_size=300), st.integers(12, 80))
def test_split_message_entities_and_limit_hold_for_any_markup_soup(text: str, limit: int) -> None:
    chunks = split_message(text, limit)

    assert all(len(chunk) <= limit for chunk in chunks)
