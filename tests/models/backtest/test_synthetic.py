"""The synthetic generator and the key sanity check: no edge on an efficient market."""

import math
from collections import Counter

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain import HistoryBatch, Market, OddsKind, Selection
from app.models import ModelInputError, devig_shin
from app.models.backtest import (
    BetFilter,
    PricingConfig,
    StakingConfig,
    SyntheticConfig,
    WalkForwardConfig,
    build_dataset,
    candidate_bets,
    generate_history,
    select_bets,
    summarize_bets,
    walk_forward,
)
from app.models.backtest.synthetic import round_robin, shin_prices
from tests.models.backtest.conftest import SMALL

# Larger than the shared fixture so the ROI interval is informative (~700 bets, ~3 s).
EFFICIENT = SyntheticConfig(leagues=4, teams_per_league=10, seasons=3, first_season=2020, seed=11)
ANY_BET = BetFilter(min_probability=0.0, min_ev=0.0, odds_min=1.01, odds_max=100.0)
OPENING_MARGIN = 0.03


@pytest.mark.parametrize("n_teams", [4, 8, 20])
def test_round_robin_is_a_valid_double_round_robin(n_teams: int) -> None:
    rounds = round_robin(n_teams)
    assert len(rounds) == 2 * (n_teams - 1)
    for pairs in rounds:
        playing = [team for pair in pairs for team in pair]
        assert sorted(playing) == list(range(n_teams))
    games = Counter(pair for pairs in rounds for pair in pairs)
    assert set(games.values()) == {1}
    assert len(games) == n_teams * (n_teams - 1)


def test_generated_history_shape(small_batch: HistoryBatch) -> None:
    matches_per_season = SMALL.teams_per_league * (SMALL.teams_per_league - 1)
    assert len(small_batch.fixtures) == SMALL.leagues * SMALL.seasons * matches_per_season
    assert len(small_batch.results) == len(small_batch.fixtures)
    kinds = Counter((o.bookmaker, o.kind) for o in small_batch.odds)
    assert set(kinds) == {
        ("pinnacle", OddsKind.OPENING),
        ("pinnacle", OddsKind.CLOSING),
        ("market_avg", OddsKind.OPENING),
        ("market_avg", OddsKind.CLOSING),
    }
    markets = {o.market.type for o in small_batch.odds}
    assert {m.value for m in markets} == {"1x2", "totals", "asian_handicap"}
    goals = [r.home_score + r.away_score for r in small_batch.results]  # type: ignore[operator]
    assert 2.0 < sum(goals) / len(goals) < 3.6  # football-like scoring


def test_generation_is_reproducible() -> None:
    config = SyntheticConfig(leagues=1, teams_per_league=4, seasons=1, seed=3)
    assert generate_history(config) == generate_history(config)


@given(
    st.lists(st.floats(0.02, 1.0), min_size=2, max_size=3).map(
        lambda raw: tuple(value / sum(raw) for value in raw)
    ),
    st.floats(0.01, 0.08),
)
def test_shin_prices_carry_the_margin_and_devig_back(
    truth: tuple[float, ...], margin: float
) -> None:
    prices = shin_prices(truth, margin)
    if min(prices) <= 1.0:
        return  # unquotable favourite: the generator skips such markets
    assert sum(1 / p for p in prices) == pytest.approx(1 + margin, abs=0.03)
    # Rounding to 2 decimals limits the recovery precision.
    assert devig_shin(prices) == pytest.approx(truth, abs=0.01)


def test_shin_prices_reject_impossible_margins() -> None:
    with pytest.raises(ModelInputError, match="margin"):
        shin_prices((0.5, 0.5), 0.0)
    with pytest.raises(ModelInputError, match="margin"):
        shin_prices((0.5, 0.5), 5.0)


@pytest.mark.parametrize(
    "change", [{"teams_per_league": 5}, {"teams_per_league": 2}, {"leagues": 0}, {"seasons": 0}]
)
def test_config_validation(change: dict[str, int]) -> None:
    with pytest.raises(ModelInputError):
        SyntheticConfig(**change)  # type: ignore[arg-type]


def test_market_prices_follow_the_true_distribution(small_batch: HistoryBatch) -> None:
    # The closing 1X2 home price must be calibrated: de-vigged favourites win as often as
    # their probability says (efficient market by construction).
    results = {r.fixture: r for r in small_batch.results}
    closing: dict[object, dict[Selection, float]] = {}
    for o in small_batch.odds:
        if (
            o.bookmaker == "pinnacle"
            and o.kind is OddsKind.CLOSING
            and o.market == Market.match_result()
        ):
            closing.setdefault(o.fixture, {})[o.selection] = o.odds
    predicted = observed = 0.0
    for ref, prices in closing.items():
        home = devig_shin([prices[s] for s in Market.match_result().selections])[0]
        result = results[ref]
        predicted += home
        observed += float(result.home_score > result.away_score)  # type: ignore[operator]
    n = len(closing)
    assert abs(predicted - observed) / n < 3 * math.sqrt(0.25 / n)


def test_backtest_finds_no_edge_on_an_efficient_market() -> None:
    """Key sanity check: if this fails, the backtest leaks information or is optimistic."""
    dataset = build_dataset(generate_history(EFFICIENT))
    run = walk_forward(dataset, WalkForwardConfig(refit_days=14), seasons={2021, 2022})
    # Bet on the pure model (w = 1) wherever it sees any value: maximal chance of fake edge.
    candidates = candidate_bets(
        run.predictions, weight=1.0, pricing=PricingConfig(), staking=StakingConfig()
    )
    bets = select_bets(candidates, ANY_BET)
    summary = summarize_bets(bets, bank=100.0, seed=1, n_resamples=1000)
    assert summary.n_bets > 600
    assert summary.roi_ci is not None
    assert summary.mean_closing_ev is not None
    assert summary.mean_clv is not None
    assert summary.beat_close_rate is not None
    # The expected return of every bet is negative: about the opening margin.
    assert -2.5 * OPENING_MARGIN < summary.mean_closing_ev < -0.5 * OPENING_MARGIN
    # Realised ROI is consistent with that expectation and shows no significant edge.
    assert summary.roi_ci.contains(summary.mean_closing_ev)
    assert summary.roi_ci.low < 0.0
    # Nobody beats an efficient close: CLV is negative, positive only through odds rounding.
    assert summary.mean_clv < 0.0
    assert summary.beat_close_rate < 0.05
