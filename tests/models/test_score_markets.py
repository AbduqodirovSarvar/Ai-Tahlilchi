import math

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from scipy.stats import poisson, skellam

from app.models import (
    AsianOutcomeProbabilities,
    ModelInputError,
    asian_expected_value,
    asian_handicap,
    both_teams_to_score,
    double_chance,
    feasible_rho,
    match_result,
    score_matrix,
    split_line,
    top_scores,
    total_goals,
)

rates = st.floats(min_value=0.1, max_value=5.0)
rhos = st.floats(min_value=-0.3, max_value=0.3)
asian_lines = st.integers(min_value=-16, max_value=16).map(lambda quarters: quarters / 4)
total_lines = st.integers(min_value=0, max_value=28).map(lambda quarters: quarters / 4)

LAM, MU = 1.6, 1.1


@pytest.fixture
def independent() -> np.ndarray:
    return score_matrix(LAM, MU, 0.0, max_goals=30)


def assert_distribution(outcomes: AsianOutcomeProbabilities) -> None:
    assert math.fsum(outcomes.as_tuple()) == pytest.approx(1.0, abs=1e-9)
    assert min(outcomes.as_tuple()) >= 0.0


def test_match_result_matches_skellam(independent: np.ndarray) -> None:
    # Home goals minus away goals of independent Poissons is Skellam distributed.
    result = match_result(independent)
    assert result.away == pytest.approx(skellam.cdf(-1, LAM, MU), abs=1e-12)
    assert result.draw == pytest.approx(skellam.pmf(0, LAM, MU), abs=1e-12)
    assert result.home == pytest.approx(skellam.sf(0, LAM, MU), abs=1e-12)


def test_totals_and_btts_match_closed_forms(independent: np.ndarray) -> None:
    # The total of independent Poissons is Poisson(lambda + mu).
    for line, at_most in [(1.5, 1), (2.5, 2), (3.5, 3)]:
        over = total_goals(independent, line).first
        assert over.win == pytest.approx(poisson.sf(at_most, LAM + MU), abs=1e-12)
        assert over.half_win == over.push == over.half_loss == 0.0
    btts = both_teams_to_score(independent)
    assert btts.yes == pytest.approx((1 - math.exp(-LAM)) * (1 - math.exp(-MU)), abs=1e-12)
    assert btts.yes + btts.no == pytest.approx(1.0, abs=1e-15)


def test_quarter_totals_hand_decomposed(independent: np.ndarray) -> None:
    total = LAM + MU
    over_225 = total_goals(independent, 2.25).first
    assert over_225.loss == pytest.approx(poisson.cdf(1, total), abs=1e-12)
    assert over_225.half_loss == pytest.approx(poisson.pmf(2, total), abs=1e-12)
    assert over_225.win == pytest.approx(poisson.sf(2, total), abs=1e-12)
    over_275 = total_goals(independent, 2.75).first
    assert over_275.loss == pytest.approx(poisson.cdf(2, total), abs=1e-12)
    assert over_275.half_win == pytest.approx(poisson.pmf(3, total), abs=1e-12)
    assert over_275.win == pytest.approx(poisson.sf(3, total), abs=1e-12)
    under_3 = total_goals(independent, 3.0).second
    assert under_3.push == pytest.approx(poisson.pmf(3, total), abs=1e-12)
    assert under_3.win == pytest.approx(poisson.cdf(2, total), abs=1e-12)


def test_asian_handicaps_hand_decomposed(independent: np.ndarray) -> None:
    result = match_result(independent)
    draw_no_bet = asian_handicap(independent, 0.0)
    assert draw_no_bet.first.as_tuple() == pytest.approx(
        (result.home, 0.0, result.draw, 0.0, result.away), abs=1e-12
    )
    minus_quarter = asian_handicap(independent, -0.25).first
    assert minus_quarter.half_loss == pytest.approx(result.draw, abs=1e-12)
    assert minus_quarter.win == pytest.approx(result.home, abs=1e-12)
    plus_quarter_away = asian_handicap(independent, -0.25).second  # away +0.25
    assert plus_quarter_away.half_win == pytest.approx(result.draw, abs=1e-12)
    minus_one = asian_handicap(independent, -1.0).first
    assert minus_one.push == pytest.approx(skellam.pmf(1, LAM, MU), abs=1e-12)
    assert minus_one.win == pytest.approx(skellam.sf(1, LAM, MU), abs=1e-12)
    minus_half = asian_handicap(independent, -0.5).first
    assert minus_half.as_tuple() == pytest.approx(
        AsianOutcomeProbabilities.binary(result.home).as_tuple(), abs=1e-12
    )


@given(rates, rates, rhos, asian_lines, total_lines)
def test_every_market_is_a_distribution(
    lam: float, mu: float, rho: float, handicap: float, total: float
) -> None:
    matrix = score_matrix(lam, mu, feasible_rho(lam, mu, rho))
    result = match_result(matrix)
    assert result.home + result.draw + result.away == pytest.approx(1.0, abs=1e-9)
    chance = double_chance(matrix)
    assert chance.home_or_draw + result.away == pytest.approx(1.0, abs=1e-9)
    assert chance.home_or_away + result.draw == pytest.approx(1.0, abs=1e-9)
    assert chance.draw_or_away + result.home == pytest.approx(1.0, abs=1e-9)
    btts = both_teams_to_score(matrix)
    assert btts.yes + btts.no == pytest.approx(1.0, abs=1e-9)
    for market in (total_goals(matrix, total), asian_handicap(matrix, handicap)):
        assert_distribution(market.first)
        assert_distribution(market.second)
        assert market.second == market.first.opposite()


@given(rates, rates, asian_lines, total_lines, st.floats(min_value=1.05, max_value=5.0))
def test_quarter_line_is_two_half_stakes(
    lam: float, mu: float, handicap: float, total: float, odds: float
) -> None:
    matrix = score_matrix(lam, mu, feasible_rho(lam, mu, -0.05))
    for market, line in ((asian_handicap, handicap), (total_goals, total)):
        low, high = split_line(line)
        whole = asian_expected_value(market(matrix, line).first, odds)
        halves = (
            asian_expected_value(market(matrix, low).first, odds)
            + asian_expected_value(market(matrix, high).first, odds)
        ) / 2
        assert whole == pytest.approx(halves, abs=1e-9)


def test_top_scores_sorted_with_deterministic_ties() -> None:
    matrix = np.zeros((3, 3))
    matrix[1, 1] = 0.4
    matrix[0, 1] = matrix[1, 0] = 0.2  # tie: (0, 1) before (1, 0)
    matrix[2, 2] = 0.2
    scores = top_scores(matrix, 3)
    assert [(s.home_goals, s.away_goals) for s in scores] == [(1, 1), (0, 1), (1, 0)]
    assert scores[0].probability == pytest.approx(0.4)
    assert len(top_scores(matrix, 100)) == 9


def test_top_scores_of_a_real_matrix(independent: np.ndarray) -> None:
    best = top_scores(independent)[0]
    assert (best.home_goals, best.away_goals) == (1, 1)
    assert best.probability == pytest.approx(poisson.pmf(1, LAM) * poisson.pmf(1, MU), abs=1e-12)


@pytest.mark.parametrize(
    "matrix",
    [
        np.full(4, 0.25),
        np.full((2, 3), 1 / 6),
        np.empty((0, 0)),
        np.array([[0.5, 0.5], [0.1, -0.1]]),
        np.array([[0.5, 0.5], [np.nan, 0.0]]),
        np.array([[0.5, 0.5], [0.5, 0.0]]),
    ],
)
def test_invalid_matrices_are_rejected(matrix: np.ndarray) -> None:
    with pytest.raises(ModelInputError):
        match_result(matrix)


def test_invalid_market_arguments_are_rejected(independent: np.ndarray) -> None:
    with pytest.raises(ModelInputError):
        total_goals(independent, -0.5)
    with pytest.raises(ModelInputError):
        total_goals(independent, 2.4)
    with pytest.raises(ModelInputError):
        asian_handicap(independent, 0.1)
    with pytest.raises(ModelInputError):
        top_scores(independent, 0)
