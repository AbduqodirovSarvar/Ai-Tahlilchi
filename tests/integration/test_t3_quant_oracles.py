"""T3 QA: model formulas checked against hand-computed or independently derived oracles.

Every expected number below was computed by hand (or by a few lines of arithmetic written
here, independent of the code under test), never by calling the function being checked.
Covered risks: wrong settlement (quarter lines), wrong EV/ROI/CLV, an ensemble that breaks
probability axioms, a market model that silently trusts the wrong bookmaker, and confidence
thresholds that are off by one at their boundaries.
"""

import math
from datetime import UTC, datetime, timedelta
from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain import (
    BOOKMAKER_MARKET_AVERAGE,
    BOOKMAKER_PINNACLE,
    ExternalRef,
    Fixture,
    League,
    Market,
    OddsKind,
    OddsSnapshot,
    Selection,
    Sport,
    Team,
)
from app.models import (
    AsianOutcomeProbabilities,
    ConfidenceInputs,
    ConfidenceLevel,
    ConfidenceReason,
    DevigMethod,
    FairMarket,
    MarketModelConfig,
    MarketProbabilities,
    MarketQuote,
    MatchRecord,
    ModelInputError,
    Settlement,
    asian_from_two_way,
    assess_confidence,
    complete_quotes,
    ensemble,
    fair_markets,
    settle_selection,
)
from app.models.backtest import (
    BacktestFixture,
    Bet,
    FixturePrediction,
    PricingConfig,
    StakingConfig,
    candidate_bets,
    summarize_bets,
)
from app.models.confidence import PRELIMINARY_DAYS, SANITY_DISAGREEMENT
from app.models.outcomes import binary_market, two_sided_market

W, HW, P, HL, L = (
    Settlement.WIN,
    Settlement.HALF_WIN,
    Settlement.PUSH,
    Settlement.HALF_LOSS,
    Settlement.LOSS,
)
H, D, A = Selection.HOME, Selection.DRAW, Selection.AWAY
OVER, UNDER = Selection.OVER, Selection.UNDER
FIXTURE_REF: Final = ExternalRef("qa", "fx-1")
TAKEN_AT: Final = datetime(2025, 3, 1, 12, 0, tzinfo=UTC)

# ----------------------------------------------------------------------------- settlement

# (market, selection, home goals, away goals, expected) - written by hand from the rules
# "half the stake on each neighbouring line" for quarter lines; not derived from the code.
SETTLEMENT_TRUTH_TABLE: Final = [
    # 1X2
    (Market.match_result(), H, 2, 1, W),
    (Market.match_result(), D, 2, 1, L),
    (Market.match_result(), A, 2, 1, L),
    (Market.match_result(), D, 1, 1, W),
    (Market.match_result(), H, 0, 0, L),
    (Market.match_result(), A, 0, 2, W),
    # double chance
    (Market.double_chance(), Selection.HOME_OR_DRAW, 1, 1, W),
    (Market.double_chance(), Selection.HOME_OR_AWAY, 1, 1, L),
    (Market.double_chance(), Selection.DRAW_OR_AWAY, 1, 1, W),
    (Market.double_chance(), Selection.HOME_OR_DRAW, 2, 0, W),
    (Market.double_chance(), Selection.HOME_OR_AWAY, 2, 0, W),
    (Market.double_chance(), Selection.DRAW_OR_AWAY, 2, 0, L),
    (Market.double_chance(), Selection.HOME_OR_DRAW, 0, 1, L),
    (Market.double_chance(), Selection.DRAW_OR_AWAY, 0, 1, W),
    # BTTS
    (Market.btts(), Selection.YES, 1, 1, W),
    (Market.btts(), Selection.NO, 1, 1, L),
    (Market.btts(), Selection.YES, 3, 0, L),
    (Market.btts(), Selection.NO, 0, 0, W),
    # totals, half lines
    (Market.totals(2.5), OVER, 2, 1, W),
    (Market.totals(2.5), UNDER, 2, 1, L),
    (Market.totals(2.5), OVER, 1, 1, L),
    (Market.totals(0.5), OVER, 0, 0, L),
    (Market.totals(0.5), UNDER, 0, 0, W),
    # totals, whole lines (push = void)
    (Market.totals(2.0), OVER, 1, 1, P),
    (Market.totals(2.0), UNDER, 1, 1, P),
    (Market.totals(2.0), OVER, 2, 1, W),
    (Market.totals(3.0), UNDER, 2, 1, P),
    (Market.totals(3.0), UNDER, 1, 1, W),
    # totals, quarter lines: 2.25 = half on 2.0 + half on 2.5
    (Market.totals(2.25), OVER, 1, 1, HL),  # 2.0 push, 2.5 loss
    (Market.totals(2.25), UNDER, 1, 1, HW),  # 2.0 push, 2.5 win
    (Market.totals(2.25), OVER, 2, 1, W),
    (Market.totals(2.25), OVER, 1, 0, L),
    # 2.75 = half on 2.5 + half on 3.0
    (Market.totals(2.75), OVER, 2, 1, HW),  # 2.5 win, 3.0 push
    (Market.totals(2.75), UNDER, 2, 1, HL),  # 2.5 loss, 3.0 push
    (Market.totals(2.75), OVER, 1, 1, L),
    (Market.totals(2.75), UNDER, 1, 1, W),
    (Market.totals(2.75), OVER, 3, 1, W),
    # Asian handicap, line applied to the home team (away gets the opposite line)
    (Market.asian_handicap(-0.5), H, 1, 0, W),
    (Market.asian_handicap(-0.5), A, 1, 0, L),
    (Market.asian_handicap(-0.5), H, 0, 0, L),
    (Market.asian_handicap(-0.5), A, 0, 0, W),
    (Market.asian_handicap(0.0), H, 1, 1, P),
    (Market.asian_handicap(0.0), A, 1, 1, P),
    (Market.asian_handicap(0.0), H, 1, 0, W),
    (Market.asian_handicap(-0.25), H, 1, 1, HL),  # 0.0 push, -0.5 loss
    (Market.asian_handicap(-0.25), A, 1, 1, HW),
    (Market.asian_handicap(-0.25), H, 2, 1, W),
    (Market.asian_handicap(0.25), H, 1, 1, HW),  # 0.0 push, +0.5 win
    (Market.asian_handicap(0.25), A, 1, 1, HL),
    (Market.asian_handicap(0.25), H, 0, 1, L),
    (Market.asian_handicap(-0.75), H, 2, 1, HW),  # -0.5 win, -1.0 push
    (Market.asian_handicap(-0.75), A, 2, 1, HL),
    (Market.asian_handicap(-0.75), H, 3, 1, W),
    (Market.asian_handicap(-0.75), H, 1, 1, L),
    (Market.asian_handicap(-0.75), A, 1, 1, W),
    (Market.asian_handicap(0.75), H, 0, 1, HL),  # +0.5 loss, +1.0 push
    (Market.asian_handicap(0.75), A, 0, 1, HW),
    (Market.asian_handicap(0.75), H, 1, 1, W),
    (Market.asian_handicap(-1.0), H, 2, 1, P),
    (Market.asian_handicap(-1.0), H, 3, 1, W),
    (Market.asian_handicap(-1.0), H, 1, 1, L),
    (Market.asian_handicap(-1.25), H, 2, 1, HL),  # -1.0 push, -1.5 loss
    (Market.asian_handicap(-1.25), A, 2, 1, HW),
    (Market.asian_handicap(-1.25), H, 3, 1, W),
    (Market.asian_handicap(-1.75), H, 3, 1, HW),  # -1.5 win, -2.0 push
    (Market.asian_handicap(-1.75), H, 2, 1, L),
    (Market.asian_handicap(1.5), H, 0, 1, W),
    (Market.asian_handicap(1.5), H, 0, 2, L),
    (Market.asian_handicap(-2.0), H, 2, 0, P),
    (Market.asian_handicap(-2.0), A, 2, 0, P),
]


@pytest.mark.parametrize(
    ("market", "selection", "home", "away", "expected"),
    SETTLEMENT_TRUTH_TABLE,
    ids=[f"{m}-{s}-{h}:{a}" for m, s, h, a, _ in SETTLEMENT_TRUTH_TABLE],
)
def test_settlement_matches_hand_written_truth_table(
    market: Market, selection: Selection, home: int, away: int, expected: Settlement
) -> None:
    assert settle_selection(market, selection, home, away) is expected


def _oracle_settle(margin_without_line: int, line: float) -> Settlement:
    """Split-stake settlement written independently: each half is win/push/lose (+1/0/-1)."""
    quarters = round(line * 4)
    halves = (quarters - 1, quarters + 1) if quarters % 2 else (quarters, quarters)
    score = 0
    for half_quarters in halves:
        margin = 4 * margin_without_line + half_quarters  # in quarter goals, exact ints
        score += (margin > 0) - (margin < 0)
    return {2: W, 1: HW, 0: P, -1: HL, -2: L}[score]


@given(
    home=st.integers(0, 9),
    away=st.integers(0, 9),
    quarters=st.integers(-14, 14),
)
def test_asian_handicap_settlement_equals_split_stake_oracle(
    home: int, away: int, quarters: int
) -> None:
    line = quarters / 4
    market = Market.asian_handicap(line)
    assert settle_selection(market, H, home, away) is _oracle_settle(home - away, line)
    assert settle_selection(market, A, home, away) is _oracle_settle(away - home, -line)


@given(home=st.integers(0, 9), away=st.integers(0, 9), quarters=st.integers(1, 30))
def test_totals_settlement_equals_split_stake_oracle(home: int, away: int, quarters: int) -> None:
    line = quarters / 4
    market = Market.totals(line)
    assert settle_selection(market, OVER, home, away) is _oracle_settle(home + away, -line)
    assert settle_selection(market, UNDER, home, away) is _oracle_settle(-(home + away), line)


# ----------------------------------------------------------------------------- EV / ROI / CLV

KICKOFF: Final = datetime(2025, 3, 1, 15, 0, tzinfo=UTC)
LEAGUE: Final = League(ref=ExternalRef("qa", "L"), sport=Sport.FOOTBALL, name="QA League")


def _fixture(number: int) -> Fixture:
    kickoff = KICKOFF + timedelta(days=number)
    return Fixture(
        ref=ExternalRef("qa", f"fx-{number}"),
        sport=Sport.FOOTBALL,
        league=LEAGUE,
        season=2024,
        kickoff=kickoff,
        home=Team(ref=ExternalRef("qa", f"h{number}"), sport=Sport.FOOTBALL, name=f"H{number}"),
        away=Team(ref=ExternalRef("qa", f"a{number}"), sport=Sport.FOOTBALL, name=f"A{number}"),
    )


def _quote(
    fixture: Fixture, market: Market, prices: dict[Selection, float], kind: OddsKind
) -> MarketQuote:
    return MarketQuote(
        fixture=fixture.ref,
        bookmaker=BOOKMAKER_PINNACLE,
        kind=kind,
        market=market,
        prices=prices,
        taken_at=fixture.kickoff,
    )


def _fair(probabilities: MarketProbabilities, kind: OddsKind) -> FairMarket:
    return FairMarket(
        probabilities=probabilities,
        bookmaker=BOOKMAKER_PINNACLE,
        kind=kind,
        overround=0.0,
        wide_uncertainty=False,
    )


def _prediction(  # noqa: PLR0913, PLR0917 - a ledger row is naturally wide
    number: int,
    score: tuple[int, int],
    probabilities: MarketProbabilities,
    opening: dict[Selection, float],
    closing: dict[Selection, float],
    closing_probabilities: MarketProbabilities,
) -> FixturePrediction:
    fixture = _fixture(number)
    market = probabilities.market
    record = MatchRecord(
        home_team=str(fixture.home.ref),
        away_team=str(fixture.away.ref),
        home_goals=score[0],
        away_goals=score[1],
        match_date=fixture.kickoff.date(),
    )
    item = BacktestFixture(
        fixture,
        record,
        (
            _quote(fixture, market, opening, OddsKind.OPENING),
            _quote(fixture, market, closing, OddsKind.CLOSING),
        ),
    )
    return FixturePrediction(
        item=item,
        as_of=fixture.kickoff.date(),
        team_matches=20,
        model={market: probabilities},
        market={market: _fair(probabilities, OddsKind.OPENING)},
        closing={market: _fair(closing_probabilities, OddsKind.CLOSING)},
    )


def _two_sided(market: Market, *first: float) -> MarketProbabilities:
    return two_sided_market(market, AsianOutcomeProbabilities(*first))


# One bet per row: (fixture no., market, side, score, model distribution of the market,
# opening prices, closing prices, closing distribution). Hand-computed values follow.
LEDGER: Final = [
    (
        1,
        H,
        (2, 1),  # 1X2 home wins in full
        binary_market(Market.match_result(), {H: 0.5, D: 0.3, A: 0.2}),
        {H: 2.50, D: 3.40, A: 4.50},
        {H: 2.30, D: 3.40, A: 4.80},
        binary_market(Market.match_result(), {H: 0.4, D: 0.3, A: 0.3}),
    ),
    (
        2,
        H,
        (1, 1),  # AH 0.0 home: push (void)
        _two_sided(Market.asian_handicap(0.0), 0.45, 0.0, 0.25, 0.0, 0.30),
        {H: 1.90, A: 1.95},
        {H: 1.80, A: 2.05},
        _two_sided(Market.asian_handicap(0.0), 0.40, 0.0, 0.25, 0.0, 0.35),
    ),
    (
        3,
        OVER,
        (1, 1),  # totals 2.25 over: total 2 -> half loss
        _two_sided(Market.totals(2.25), 0.40, 0.10, 0.0, 0.20, 0.30),
        {OVER: 2.00, UNDER: 1.85},
        {OVER: 2.10, UNDER: 1.78},
        _two_sided(Market.totals(2.25), 0.35, 0.10, 0.0, 0.20, 0.35),
    ),
    (
        4,
        H,
        (2, 1),  # AH -0.75 home: wins by one -> half win
        _two_sided(Market.asian_handicap(-0.75), 0.30, 0.20, 0.0, 0.0, 0.50),
        {H: 2.20, A: 1.70},
        {H: 2.00, A: 1.85},
        _two_sided(Market.asian_handicap(-0.75), 0.30, 0.20, 0.0, 0.0, 0.50),
    ),
    (
        5,
        UNDER,
        (2, 2),  # totals 2.5 under: 4 goals -> loss
        binary_market(Market.totals(2.5), {OVER: 0.4, UNDER: 0.6}),
        {OVER: 2.30, UNDER: 1.80},
        {OVER: 2.20, UNDER: 1.85},
        binary_market(Market.totals(2.5), {OVER: 0.45, UNDER: 0.55}),
    ),
]
# EV = sum p_k * r_k with r = (o-1, (o-1)/2, 0, -1/2, -1), computed by hand:
#  1: 0.5*1.5 - 0.5            = +0.25
#  2: 0.45*0.9 - 0.30          = +0.105
#  3: 0.40 + 0.10*0.5 - 0.20*0.5 - 0.30 = +0.05
#  4: 0.30*1.2 + 0.20*0.6 - 0.50       = -0.02
#  5: 0.6*0.8 - 0.4            = +0.08
EXPECTED_EV: Final = (0.25, 0.105, 0.05, -0.02, 0.08)
EXPECTED_SETTLEMENT: Final = (W, P, HL, HW, L)
EXPECTED_PROFIT: Final = (1.50, 0.0, -0.5, 0.60, -1.0)
# Closing fair odds o* = 1 + (loss + half_loss/2) / (win + half_win/2):
#  1: 1/0.4 = 2.5                  -> CLV = ln(2.50/2.5)     = 0
#  2: 1 + 0.35/0.40 = 1.875        -> CLV = ln(1.90/1.875)
#  3: 1 + 0.45/0.40 = 2.125        -> CLV = ln(2.00/2.125)
#  4: 1 + 0.50/0.40 = 2.25         -> CLV = ln(2.20/2.25)
#  5: 1/0.55                       -> CLV = ln(1.80*0.55)
EXPECTED_CLV: Final = (
    0.0,
    math.log(1.90 / 1.875),
    math.log(2.00 / 2.125),
    math.log(2.20 / 2.25),
    math.log(1.80 * 0.55),
)
# Same bet struck at the closing price of the same bookmaker.
EXPECTED_CLOSING_PROFIT: Final = (1.30, 0.0, -0.5, 0.50, -1.0)


@pytest.fixture(scope="module")
def ledger_bets() -> list[Bet]:
    predictions = [
        _prediction(number, score, probs, opening, closing, closing_probs)
        for number, _, score, probs, opening, closing, closing_probs in LEDGER
    ]
    candidates = candidate_bets(
        predictions, weight=1.0, pricing=PricingConfig(), staking=StakingConfig()
    )
    wanted = {(f"qa:fx-{number}", side) for number, side, *_ in LEDGER}
    bets = [bet for bet in candidates if (bet.fixture, bet.selection) in wanted]
    assert [bet.fixture for bet in bets] == [f"qa:fx-{n}" for n, *_ in LEDGER]
    return bets


def test_ledger_ev_settlement_profit_and_clv_match_hand_values(ledger_bets: list[Bet]) -> None:
    for bet, ev, settlement, profit, clv, closing_profit in zip(
        ledger_bets,
        EXPECTED_EV,
        EXPECTED_SETTLEMENT,
        EXPECTED_PROFIT,
        EXPECTED_CLV,
        EXPECTED_CLOSING_PROFIT,
        strict=True,
    ):
        assert bet.ev == pytest.approx(ev, abs=1e-12), bet
        assert bet.settlement is settlement, bet
        assert bet.flat_stake == 1.0
        assert bet.flat_profit == pytest.approx(profit, abs=1e-12), bet
        assert bet.clv == pytest.approx(clv, abs=1e-12), bet
        assert bet.closing_profit == pytest.approx(closing_profit, abs=1e-12), bet
        # Kelly profit is the Kelly stake times the same per-unit return.
        assert bet.kelly_profit == pytest.approx(bet.kelly_stake * profit, abs=1e-12)
        assert 0.0 <= bet.kelly_stake <= 2.0  # 2% cap of the 100-unit bank


def test_ledger_summary_roi_drawdown_and_hit_rate_match_hand_values(
    ledger_bets: list[Bet],
) -> None:
    summary = summarize_bets(ledger_bets, bank=100.0, seed=1, n_resamples=200)
    # Turnover counts every placed bet, the void one included (stake 1 each): 5 units.
    assert summary.n_bets == 5
    assert summary.turnover == pytest.approx(5.0)
    assert summary.profit == pytest.approx(0.6)  # 1.5 + 0 - 0.5 + 0.6 - 1
    assert summary.roi == pytest.approx(0.12)
    assert summary.hit_rate == pytest.approx(0.4)  # the win and the half win
    # Equity 0 -> 1.5 -> 1.5 -> 1.0 -> 1.6 -> 0.6: deepest fall 1.6 -> 0.6.
    assert summary.max_drawdown == pytest.approx(1.0)
    assert summary.mean_clv == pytest.approx(sum(EXPECTED_CLV) / 5, abs=1e-12)
    assert summary.beat_close_rate == pytest.approx(0.2)  # only bet 2 beat the close
    assert summary.roi_at_closing == pytest.approx(sum(EXPECTED_CLOSING_PROFIT) / 5)
    assert summary.roi_ci is not None
    assert summary.roi_ci.low <= summary.roi <= summary.roi_ci.high


def test_clv_from_a_shin_devigged_two_way_closing_quote() -> None:
    # Two-way Shin equals the additive method: p_i = pi_i - (booksum - 1) / 2 (ADR 0003).
    closing = {OVER: 1.80, UNDER: 2.10}
    booksum = 1 / 1.80 + 1 / 2.10
    p_over = 1 / 1.80 - (booksum - 1) / 2
    quote = MarketQuote(
        fixture=FIXTURE_REF,
        bookmaker=BOOKMAKER_PINNACLE,
        kind=OddsKind.CLOSING,
        market=Market.totals(2.5),
        prices=closing,
        taken_at=TAKEN_AT,
    )
    fair = fair_markets([quote], kind=OddsKind.CLOSING)[Market.totals(2.5)]
    assert fair.probabilities.win_probability(OVER) == pytest.approx(p_over, abs=1e-10)
    # A bet at 1.90 on the over: CLV = ln(1.90 * p_over) = ln(1.90 / 1.852941...) > 0.
    assert p_over == pytest.approx(0.5396825, abs=1e-6)
    assert math.log(1.90 * p_over) == pytest.approx(0.025079, abs=1e-6)


# ----------------------------------------------------------------------------- ensemble

_distribution = st.lists(st.floats(0.0, 1.0), min_size=5, max_size=5).filter(
    lambda raw: sum(raw) > 1e-3
)


def _normalised(raw: list[float]) -> AsianOutcomeProbabilities:
    total = math.fsum(raw)
    return AsianOutcomeProbabilities(*(value / total for value in raw))


@settings(max_examples=200)
@given(
    model=_distribution,
    market=_distribution,
    weight=st.floats(0.0, 1.0),
    # whole and quarter lines (half lines are binary and covered by the 1X2 property)
    quarters=st.integers(-12, 12).filter(lambda q: q % 4 != 2),
)
def test_ensemble_keeps_two_sided_asian_lines_consistent(
    model: list[float], market: list[float], weight: float, quarters: int
) -> None:
    line_market = Market.asian_handicap(quarters / 4)
    mixed = ensemble(
        two_sided_market(line_market, _normalised(model)),
        two_sided_market(line_market, _normalised(market)),
        weight,
    )
    home, away = mixed.outcomes[H], mixed.outcomes[A]
    assert away == home.opposite()
    for value in (*home.as_tuple(), *away.as_tuple()):
        assert 0.0 <= value <= 1.0
    assert math.fsum(home.as_tuple()) == pytest.approx(1.0, abs=1e-12)
    # Independent oracle: the linear mix outcome by outcome.
    expected = [
        weight * m + (1 - weight) * k
        for m, k in zip(_normalised(model).as_tuple(), _normalised(market).as_tuple(), strict=True)
    ]
    assert home.as_tuple() == pytest.approx(expected, abs=1e-12)


_three_way = st.lists(st.floats(0.001, 1.0), min_size=3, max_size=3).map(
    lambda raw: [value / math.fsum(raw) for value in raw]
)


@settings(max_examples=200)
@given(model=_three_way, market=_three_way, weight=st.floats(0.0, 1.0))
def test_ensemble_of_1x2_is_a_probability_vector_and_the_linear_mix(
    model: list[float], market: list[float], weight: float
) -> None:
    result = Market.match_result()
    mixed = ensemble(
        binary_market(result, dict(zip((H, D, A), model, strict=True))),
        binary_market(result, dict(zip((H, D, A), market, strict=True))),
        weight,
    )
    wins = [mixed.win_probability(selection) for selection in (H, D, A)]
    assert all(0.0 <= value <= 1.0 for value in wins)
    assert math.fsum(wins) == pytest.approx(1.0, abs=1e-12)
    expected = [weight * m + (1 - weight) * k for m, k in zip(model, market, strict=True)]
    assert wins == pytest.approx(expected, abs=1e-9)


def test_ensemble_rejects_weights_outside_the_unit_interval() -> None:
    market = binary_market(Market.btts(), {Selection.YES: 0.5, Selection.NO: 0.5})
    for weight in (-0.01, 1.01, math.nan):
        with pytest.raises(ModelInputError):
            ensemble(market, market, weight)


@settings(max_examples=200)
@given(
    reference=_distribution,
    first=st.floats(0.02, 0.98),
)
def test_two_way_asian_market_prices_both_sides_fairly(
    reference: list[float], first: float
) -> None:
    # The decisive split must make both sides zero-EV at their de-vigged fair odds 1/p.
    outcome = asian_from_two_way(first, _normalised(reference))
    win, half_win, _, half_loss, loss = outcome.as_tuple()
    for side_win, side_half_win, side_half_loss, side_loss, p in (
        (win, half_win, half_loss, loss, first),
        (loss, half_loss, half_win, win, 1 - first),
    ):
        odds = 1 / p
        ev = side_win * (odds - 1) + side_half_win * (odds - 1) / 2 - side_half_loss / 2 - side_loss
        assert ev == pytest.approx(0.0, abs=1e-9)


# ----------------------------------------------------------------------------- market model


def _snapshots(
    bookmaker: str, market: Market, prices: dict[Selection, float], kind: OddsKind
) -> list[OddsSnapshot]:
    return [
        OddsSnapshot(
            fixture=FIXTURE_REF,
            bookmaker=bookmaker,
            market=market,
            selection=selection,
            odds=price,
            taken_at=TAKEN_AT,
            kind=kind,
        )
        for selection, price in prices.items()
    ]


PINNACLE_1X2: Final = {H: 2.00, D: 3.50, A: 4.00}
AVERAGE_1X2: Final = {H: 1.90, D: 3.30, A: 3.80}
PROPORTIONAL: Final = MarketModelConfig(method=DevigMethod.PROPORTIONAL)


def _proportional(prices: dict[Selection, float]) -> list[float]:
    implied = [1 / prices[selection] for selection in (H, D, A)]
    return [value / sum(implied) for value in implied]


def _fair_1x2(snapshots: list[OddsSnapshot]) -> FairMarket | None:
    fair = fair_markets(complete_quotes(snapshots), kind=OddsKind.OPENING, config=PROPORTIONAL)
    return fair.get(Market.match_result())


def test_market_model_prefers_pinnacle() -> None:
    fair = _fair_1x2(
        _snapshots(BOOKMAKER_MARKET_AVERAGE, Market.match_result(), AVERAGE_1X2, OddsKind.OPENING)
        + _snapshots(BOOKMAKER_PINNACLE, Market.match_result(), PINNACLE_1X2, OddsKind.OPENING)
    )
    assert fair is not None
    assert fair.bookmaker == BOOKMAKER_PINNACLE
    assert not fair.wide_uncertainty
    # 1/2 + 1/3.5 + 1/4 = 1.0357...: 0.48276, 0.27586, 0.24138.
    wins = [fair.probabilities.win_probability(s) for s in (H, D, A)]
    assert wins == pytest.approx(_proportional(PINNACLE_1X2), abs=1e-12)
    assert wins == pytest.approx([0.482759, 0.275862, 0.241379], abs=1e-6)
    assert fair.overround == pytest.approx(1 / 2 + 1 / 3.5 + 1 / 4 - 1)


def test_market_model_falls_back_to_market_average_with_wide_uncertainty() -> None:
    fair = _fair_1x2(
        _snapshots(BOOKMAKER_MARKET_AVERAGE, Market.match_result(), AVERAGE_1X2, OddsKind.OPENING)
    )
    assert fair is not None
    assert fair.bookmaker == BOOKMAKER_MARKET_AVERAGE
    assert fair.wide_uncertainty
    wins = [fair.probabilities.win_probability(s) for s in (H, D, A)]
    assert wins == pytest.approx(_proportional(AVERAGE_1X2), abs=1e-12)


def test_incomplete_pinnacle_set_is_rejected_and_the_fallback_is_used() -> None:
    partial = {H: 2.00, D: 3.50}  # away price missing: cannot be de-vigged
    snapshots = _snapshots(
        BOOKMAKER_PINNACLE, Market.match_result(), partial, OddsKind.OPENING
    ) + _snapshots(BOOKMAKER_MARKET_AVERAGE, Market.match_result(), AVERAGE_1X2, OddsKind.OPENING)
    assert {q.bookmaker for q in complete_quotes(snapshots)} == {BOOKMAKER_MARKET_AVERAGE}
    fair = _fair_1x2(snapshots)
    assert fair is not None
    assert fair.bookmaker == BOOKMAKER_MARKET_AVERAGE
    assert fair.wide_uncertainty


def test_only_untrusted_or_wrong_moment_prices_give_no_market_estimate() -> None:
    snapshots = (
        _snapshots("bet365", Market.match_result(), PINNACLE_1X2, OddsKind.OPENING)
        + _snapshots("market_max", Market.match_result(), PINNACLE_1X2, OddsKind.OPENING)
        + _snapshots(BOOKMAKER_PINNACLE, Market.match_result(), PINNACLE_1X2, OddsKind.CLOSING)
    )
    assert _fair_1x2(snapshots) is None
    assert (
        _fair_1x2(_snapshots(BOOKMAKER_PINNACLE, Market.match_result(), {H: 2.0}, OddsKind.OPENING))
        is None
    )


def test_a_partial_quote_cannot_be_constructed() -> None:
    with pytest.raises(ModelInputError, match="must price"):
        MarketQuote(
            fixture=FIXTURE_REF,
            bookmaker=BOOKMAKER_PINNACLE,
            kind=OddsKind.OPENING,
            market=Market.match_result(),
            prices={H: 2.0, D: 3.5},
            taken_at=TAKEN_AT,
        )


# ----------------------------------------------------------------------------- confidence


def _confidence(**overrides: object) -> tuple[ConfidenceLevel, tuple[ConfidenceReason, ...]]:
    fields: dict[str, object] = {
        "min_team_matches": 20,
        "xg_available": True,
        "lineups_known": True,
        "model_probability": 0.50,
        "market_probability": 0.50,
        "days_to_kickoff": 1.0,
    }
    fields.update(overrides)
    assessment = assess_confidence(ConfidenceInputs(**fields))  # type: ignore[arg-type]
    return assessment.level, assessment.reasons


@pytest.mark.parametrize(
    ("overrides", "level", "reasons"),
    [
        ({}, ConfidenceLevel.HIGH, ()),
        # team-match thresholds: 6 is enough to predict, 15 is "good"
        ({"min_team_matches": 15}, ConfidenceLevel.HIGH, ()),
        ({"min_team_matches": 14}, ConfidenceLevel.MEDIUM, (ConfidenceReason.THIN_DATA,)),
        ({"min_team_matches": 6}, ConfidenceLevel.MEDIUM, (ConfidenceReason.THIN_DATA,)),
        ({"min_team_matches": 5}, ConfidenceLevel.LOW, (ConfidenceReason.INSUFFICIENT_DATA,)),
        # days to kickoff: exactly 3 days is not "far"; anything above is
        ({"days_to_kickoff": PRELIMINARY_DAYS}, ConfidenceLevel.HIGH, ()),
        ({"days_to_kickoff": 3.0001}, ConfidenceLevel.MEDIUM, (ConfidenceReason.FAR_FROM_KICKOFF,)),
        ({"days_to_kickoff": -2.0}, ConfidenceLevel.HIGH, ()),
        # disagreement: 0.10 vs 0.25 is exactly 0.15 in binary floating point too
        (
            {"model_probability": 0.25, "market_probability": 0.10},
            ConfidenceLevel.MEDIUM,
            (ConfidenceReason.MODEL_MARKET_DISAGREEMENT,),
        ),
        (
            {"model_probability": 0.2501, "market_probability": 0.10},
            ConfidenceLevel.LOW,
            (ConfidenceReason.SANITY_DISAGREEMENT,),
        ),
        # 0.5 vs 0.58 is just under 0.08 in floats; 0.5 vs 0.5801 is above
        ({"model_probability": 0.58}, ConfidenceLevel.HIGH, ()),
        (
            {"model_probability": 0.5801},
            ConfidenceLevel.MEDIUM,
            (ConfidenceReason.MODEL_MARKET_DISAGREEMENT,),
        ),
        # calibration bias: 0.05 is tolerated, more is not
        ({"calibration_bias": 0.05}, ConfidenceLevel.HIGH, ()),
        (
            {"calibration_bias": -0.0501},
            ConfidenceLevel.MEDIUM,
            (ConfidenceReason.CALIBRATION_BIAS,),
        ),
        (
            {"market_probability": None},
            ConfidenceLevel.MEDIUM,
            (ConfidenceReason.NO_MARKET_REFERENCE,),
        ),
        (
            {"market_wide_uncertainty": True},
            ConfidenceLevel.MEDIUM,
            (ConfidenceReason.WIDE_MARKET_UNCERTAINTY,),
        ),
        (
            {"min_team_matches": 10, "xg_available": False, "lineups_known": False},
            ConfidenceLevel.LOW,
            (ConfidenceReason.THIN_DATA, ConfidenceReason.LIMITED_INFORMATION),
        ),
    ],
)
def test_confidence_thresholds_at_their_exact_boundaries(
    overrides: dict[str, object], level: ConfidenceLevel, reasons: tuple[ConfidenceReason, ...]
) -> None:
    assert _confidence(**overrides) == (level, reasons)


def test_sanity_flag_is_raised_only_above_fifteen_points() -> None:
    flagged = assess_confidence(
        ConfidenceInputs(
            min_team_matches=20,
            xg_available=True,
            lineups_known=True,
            model_probability=0.75,
            market_probability=0.55,
            days_to_kickoff=0.5,
        )
    )
    assert flagged.sanity_flag
    assert flagged.level is ConfidenceLevel.LOW
    assert flagged.disagreement == pytest.approx(0.20)


def _two_decimal_pairs(gap_hundredths: int) -> list[tuple[float, float]]:
    return [(low / 100, (low + gap_hundredths) / 100) for low in range(101 - gap_hundredths)]


def test_an_exact_fifteen_point_gap_is_judged_the_same_for_every_pair() -> None:
    levels = {
        pair: _confidence(model_probability=pair[1], market_probability=pair[0])
        for pair in _two_decimal_pairs(round(SANITY_DISAGREEMENT * 100))
    }
    # e.g. (0.35, 0.50) -> LOW + sanity flag, (0.10, 0.25) -> MEDIUM.
    assert len(set(levels.values())) == 1, levels


def test_an_exact_eight_point_gap_is_judged_the_same_for_every_pair() -> None:
    levels = {
        pair: _confidence(model_probability=pair[1], market_probability=pair[0])
        for pair in _two_decimal_pairs(8)
    }
    assert len(set(levels.values())) == 1, levels
