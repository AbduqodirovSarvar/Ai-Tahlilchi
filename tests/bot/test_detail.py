"""📋 Batafsil: formatter units and the card <-> detail round trip through the dispatcher."""

from dataclasses import replace
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.bot import texts
from app.bot.callbacks import CardCb
from app.bot.detail_formatters import (
    LABEL_WIDTH,
    MAX_MARKET_ROWS,
    form_line,
    format_fixture_detail,
    market_row,
    short_selection,
)
from app.bot.formatters import TELEGRAM_TEXT_LIMIT
from app.domain import Market, Selection, Sport
from app.services.errors import NotFoundError
from app.services.interfaces import (
    Absence,
    FixtureDetail,
    FormResult,
    HeadToHead,
    MarketQuote,
    OddsMove,
    ScoreProbability,
    TeamSide,
    TeamStats,
)
from tests.bot.fakes import ADMIN_ID, BotHarness, answers, sent_texts
from tests.bot.samples import KICKOFF, NOW
from tests.e2e.bot_client import telegram_html_problems

TASHKENT = ZoneInfo("Asia/Tashkent")
W, D, L = FormResult.WIN, FormResult.DRAW, FormResult.LOSS


def make_detail(**overrides: object) -> FixtureDetail:
    base = FixtureDetail(
        fixture_id="fx1",
        sport=Sport.FOOTBALL,
        league="Premier League",
        home_team="Arsenal",
        away_team="Chelsea",
        kickoff=KICKOFF,
        home_form=(W, W, D, L, W),
        away_form=(L, D, W),
        home_stats=TeamStats(
            matches=10,
            goals_for_avg=1.9,
            goals_against_avg=0.8,
            xg_for_avg=1.75,
            xg_against_avg=1.0,
        ),
        away_stats=TeamStats(matches=9, goals_for_avg=1.2, goals_against_avg=1.1),
        head_to_head=(
            HeadToHead(
                kickoff=KICKOFF - timedelta(days=200),
                home_team="Chelsea",
                away_team="Arsenal",
                home_score=1,
                away_score=2,
            ),
        ),
        absences=(
            Absence(side=TeamSide.HOME, player="Saka <img>", reason="jarohat & tekshiruv"),
            Absence(side=TeamSide.AWAY, player="James", reason="diskvalifikatsiya"),
        ),
        top_scores=(
            ScoreProbability(home_goals=2, away_goals=1, probability=0.11),
            ScoreProbability(home_goals=1, away_goals=1, probability=0.10),
            ScoreProbability(home_goals=2, away_goals=0, probability=0.09),
        ),
        markets=(
            MarketQuote(
                market=Market.match_result(),
                selection=Selection.HOME,
                probability=0.61,
                odds=1.85,
                ev=0.129,
            ),
            MarketQuote(market=Market.totals(2.5), selection=Selection.OVER, probability=0.55),
        ),
        odds_moves=(
            OddsMove(
                market=Market.match_result(), selection=Selection.HOME, opening=1.95, current=1.85
            ),
        ),
        lineups_confirmed=True,
        data_as_of=NOW,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- form
def test_form_line_uses_the_plan_symbols() -> None:
    assert form_line((W, W, D, L, W)) == "✅✅➖❌✅"
    assert form_line(()) == texts.DETAIL_NO_DATA


# --------------------------------------------------------------------------- golden card
def test_detail_card_contains_every_section_of_the_plan() -> None:
    text = format_fixture_detail(make_detail(), TASHKENT)

    assert "📋 <b>Arsenal — Chelsea</b>" in text
    assert "🏆 Premier League · 🕒 22.10 20:00" in text
    assert texts.DETAIL_LINEUPS_CONFIRMED in text
    assert "Arsenal: ✅✅➖❌✅" in text
    assert "Chelsea: ❌➖✅" in text
    assert "Arsenal (uyda, 10 o'yin): urgan 1.90 · o'tkazgan 0.80 · xG 1.75 / 1.00" in text
    assert "Chelsea (safarda, 9 o'yin): urgan 1.20 · o'tkazgan 1.10\n" in text + "\n"
    assert "05.04.2026 · Chelsea 1:2 Arsenal" in text
    assert "Saka &lt;img&gt;" in text
    assert "jarohat &amp; tekshiruv" in text
    assert "2:1 — 11%  ·  1:1 — 10%  ·  2:0 — 9%" in text
    assert "1X2 · 1" in text
    assert "Total · Ko'p 2.5" in text
    assert "• 1X2 · 1: 1.95 → 1.85 (-5.1%)" in text
    assert "📦 Ma'lumot 22.10 14:32 holatiga" in text
    assert telegram_html_problems(text) == []


def test_market_row_aligns_model_fair_book_and_ev() -> None:
    row = market_row(
        MarketQuote(
            market=Market.match_result(),
            selection=Selection.HOME,
            probability=0.61,
            odds=1.85,
            ev=0.129,
        )
    )

    assert row.split() == ["1X2", "·", "1", "61%", "1.64", "1.85", "+12.9%"]
    assert row.index("61%") == LABEL_WIDTH + 1 + 2  # right-aligned in a 5-wide column


def test_market_row_without_a_price_shows_dashes() -> None:
    row = market_row(MarketQuote(market=Market.btts(), selection=Selection.YES, probability=0.5))

    assert row.split()[-2:] == ["—", "—"]


@pytest.mark.parametrize(
    ("market", "selection", "expected"),
    [
        (Market.match_result(), Selection.DRAW, "X"),
        (Market.double_chance(), Selection.HOME_OR_AWAY, "12"),
        (Market.totals(2.5), Selection.UNDER, "Kam 2.5"),
        (Market.btts(), Selection.NO, "Ikkalasi gol: yo'q"),
        (Market.asian_handicap(-0.5), Selection.HOME, "Fora 1 (-0.5)"),
        (Market.asian_handicap(-0.5), Selection.AWAY, "Fora 2 (+0.5)"),
    ],
)
def test_short_selection_names(market: Market, selection: Selection, expected: str) -> None:
    assert short_selection(market, selection) == expected


# --------------------------------------------------------------------------- states and limits
def test_finished_match_shows_the_score_instead_of_lineups() -> None:
    text = format_fixture_detail(make_detail(home_score=2, away_score=1), TASHKENT)

    assert "🏁 Hisob: <b>2:1</b>" in text
    assert texts.DETAIL_LINEUPS_CONFIRMED not in text


def test_unknown_lineups_and_empty_sections_are_designed() -> None:
    bare = make_detail(
        lineups_confirmed=False,
        home_form=(),
        away_form=(),
        head_to_head=(),
        absences=(),
        top_scores=(),
        markets=(),
        odds_moves=(),
        data_as_of=None,
    )

    text = format_fixture_detail(bare, TASHKENT)

    for line in (
        texts.DETAIL_LINEUPS_UNKNOWN,
        texts.DETAIL_H2H_EMPTY,
        texts.DETAIL_ABSENCES_NONE,
        texts.DETAIL_MARKETS_EMPTY,
    ):
        assert line in text
    assert "ma'lumot yo'q" in text
    assert "Ma'lumot 22" not in text
    assert telegram_html_problems(text) == []


def test_a_huge_card_still_fits_one_message() -> None:
    markets = tuple(
        MarketQuote(
            market=Market.totals(0.5 + line * 0.25),
            selection=Selection.OVER,
            probability=0.5,
            odds=2.0,
            ev=0.0,
        )
        for line in range(60)
    )
    absences = tuple(
        Absence(side=TeamSide.HOME, player=f"Player {i} " + "x" * 80, reason="r" * 80)
        for i in range(40)
    )
    detail = make_detail(
        markets=markets,
        absences=absences,
        head_to_head=make_detail().head_to_head * 30,
        home_team="H" * 300,
    )

    text = format_fixture_detail(detail, TASHKENT)

    assert len(text) <= TELEGRAM_TEXT_LIMIT
    assert telegram_html_problems(text) == []
    assert "…va yana" in text


@given(
    team=st.text(max_size=60),
    player=st.text(max_size=60),
    rows=st.integers(min_value=0, max_value=MAX_MARKET_ROWS + 10),
)
def test_hostile_names_never_break_html_or_the_limit(team: str, player: str, rows: int) -> None:
    quotes = tuple(
        MarketQuote(market=Market.totals(0.5 + i * 0.25), selection=Selection.OVER, probability=0.5)
        for i in range(rows)
    )
    detail = make_detail(
        home_team=team or "A",
        absences=(Absence(side=TeamSide.HOME, player=player, reason="<b>"),),
        markets=quotes,
    )

    text = format_fixture_detail(detail, TASHKENT)

    assert len(text) <= TELEGRAM_TEXT_LIMIT
    assert telegram_html_problems(text) == []


# --------------------------------------------------------------------------- through the dispatcher
async def run_and_get_card(harness: BotHarness) -> tuple[int, int]:
    await harness.send("/tahlil 7kun 50% ev3%")
    await harness.press(texts.BTN_START)
    message_id, data = harness.find_button(texts.BTN_DETAIL)
    return message_id, CardCb.unpack(data).signal_id


async def test_detail_replaces_the_card_and_back_restores_it(harness: BotHarness) -> None:
    message_id, signal_id = await run_and_get_card(harness)
    card_text = harness.texts[message_id]

    methods = await harness.press_data(
        CardCb(action="detail", signal_id=signal_id).pack(), message_id
    )

    assert len(answers(methods)) == 1
    (detail_text,) = sent_texts(methods)
    assert "Forma" in detail_text
    assert "Barcha bozorlar" in detail_text
    assert telegram_html_problems(detail_text) == []
    _, back = harness.find_button(texts.BTN_BACK_TO_CARD)
    assert CardCb.unpack(back) == CardCb(action="card", signal_id=signal_id)

    methods = await harness.press_data(back, message_id)

    (restored,) = sent_texts(methods)
    assert restored == card_text
    harness.find_button(texts.BTN_DETAIL)  # the card's own buttons are back


async def test_detail_twice_is_harmless(harness: BotHarness) -> None:
    message_id, signal_id = await run_and_get_card(harness)
    data = CardCb(action="detail", signal_id=signal_id).pack()

    await harness.press_data(data, message_id)
    methods = await harness.press_data(data, message_id)

    assert len(answers(methods)) == 1


async def test_detail_of_a_vanished_signal_explains(harness: BotHarness) -> None:
    message_id, _ = await run_and_get_card(harness)

    methods = await harness.press_data(CardCb(action="detail", signal_id=99_999).pack(), message_id)

    assert texts.NOT_FOUND in [a.text for a in answers(methods)] + sent_texts(methods)


async def test_other_users_cannot_open_the_detail_of_my_signal(harness: BotHarness) -> None:
    message_id, signal_id = await run_and_get_card(harness)
    with pytest.raises(NotFoundError):
        await harness.services.signals.get_signal(ADMIN_ID + 1, signal_id)
    assert message_id


async def test_detail_of_a_retro_card_shows_the_result(harness: BotHarness) -> None:
    await harness.send("/tahlil 15.10-19.10 30% ev0%")
    await harness.press(texts.BTN_START)
    message_id, data = harness.find_button(texts.BTN_DETAIL)

    methods = await harness.press_data(data, message_id)

    (text,) = sent_texts(methods)
    assert "Forma" in text
    assert telegram_html_problems(text) == []
