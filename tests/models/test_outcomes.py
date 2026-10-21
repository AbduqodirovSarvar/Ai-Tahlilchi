import math
from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain import Market, Selection
from app.models import (
    MarketProbabilities,
    MatchRecord,
    ModelInputError,
    Settlement,
    UnknownTeamError,
    fit_dixon_coles,
    is_partition_market,
    market_from_matrix,
    predict_markets,
    score_matrix,
    settle_selection,
)
from app.models.asian import AsianOutcomeProbabilities
from app.models.outcomes import binary_market, is_half_line

MATRIX = score_matrix(1.6, 1.1, -0.08)
ALL_MARKETS = (
    Market.match_result(),
    Market.double_chance(),
    Market.btts(),
    Market.totals(2.5),
    Market.totals(2.25),
    Market.totals(3.0),
    Market.asian_handicap(-0.75),
    Market.asian_handicap(0.5),
)


@pytest.mark.parametrize("market", ALL_MARKETS, ids=str)
def test_every_market_is_a_valid_distribution(market: Market) -> None:
    probabilities = market_from_matrix(MATRIX, market)
    assert tuple(probabilities.outcomes) == market.selections
    for outcome in probabilities.outcomes.values():
        assert math.fsum(outcome.as_tuple()) == pytest.approx(1.0, abs=1e-9)
    if is_partition_market(market):
        total = math.fsum(probabilities.win_probability(s) for s in market.selections)
        assert total == pytest.approx(1.0, abs=1e-9)


def test_double_chance_selections_overlap_and_sum_to_two() -> None:
    probabilities = market_from_matrix(MATRIX, Market.double_chance())
    total = math.fsum(probabilities.win_probability(s) for s in Market.double_chance().selections)
    assert total == pytest.approx(2.0)


def test_two_sided_lines_are_mirror_images() -> None:
    handicap = market_from_matrix(MATRIX, Market.asian_handicap(-0.75))
    assert handicap.outcomes[Selection.AWAY] == handicap.outcomes[Selection.HOME].opposite()
    assert handicap.outcomes[Selection.HOME].half_win > 0  # home wins by exactly one


@pytest.mark.parametrize(
    ("market", "expected"),
    [
        (Market.match_result(), True),
        (Market.btts(), True),
        (Market.totals(2.5), True),
        (Market.asian_handicap(-1.5), True),
        (Market.totals(2.0), False),
        (Market.asian_handicap(-0.25), False),
        (Market.double_chance(), False),
    ],
    ids=str,
)
def test_partition_markets(market: Market, expected: bool) -> None:
    assert is_partition_market(market) is expected


def test_is_half_line_handles_negative_lines() -> None:
    assert is_half_line(-1.5)
    assert not is_half_line(-1.0)
    assert not is_half_line(-1.25)


def test_market_probabilities_validation() -> None:
    with pytest.raises(ModelInputError, match="needs outcomes"):
        MarketProbabilities(Market.btts(), {Selection.YES: AsianOutcomeProbabilities.binary(1.0)})
    with pytest.raises(ModelInputError, match="sum to 1"):
        binary_market(Market.btts(), {Selection.YES: 0.6, Selection.NO: 0.6})
    probabilities = market_from_matrix(MATRIX, Market.btts())
    with pytest.raises(ValueError, match="not valid"):
        probabilities.win_probability(Selection.HOME)


def test_predict_markets_uses_one_matrix_for_all_markets() -> None:
    teams = ("a", "b", "c", "d")
    matches = [
        MatchRecord(home, away, (i + j) % 3, (i * j) % 2, date(2024, 1, 1 + (i * 4 + j) % 27))
        for i, home in enumerate(teams)
        for j, away in enumerate(teams)
        if home != away
    ] * 2
    model = fit_dixon_coles(matches, date(2024, 2, 1), xi=0.0)
    markets = [Market.match_result(), Market.totals(2.5), Market.match_result()]
    predicted = predict_markets(model, "a", "b", markets)
    assert list(predicted) == [Market.match_result(), Market.totals(2.5)]
    expected = market_from_matrix(model.score_matrix("a", "b"), Market.match_result())
    assert predicted[Market.match_result()] == expected
    with pytest.raises(UnknownTeamError):
        predict_markets(model, "a", "zzz", markets)


@pytest.mark.parametrize(
    ("market", "selection", "score", "expected"),
    [
        (Market.match_result(), Selection.HOME, (2, 1), Settlement.WIN),
        (Market.match_result(), Selection.DRAW, (2, 1), Settlement.LOSS),
        (Market.match_result(), Selection.DRAW, (1, 1), Settlement.WIN),
        (Market.match_result(), Selection.AWAY, (0, 3), Settlement.WIN),
        (Market.double_chance(), Selection.HOME_OR_DRAW, (1, 1), Settlement.WIN),
        (Market.double_chance(), Selection.HOME_OR_AWAY, (1, 1), Settlement.LOSS),
        (Market.double_chance(), Selection.DRAW_OR_AWAY, (0, 1), Settlement.WIN),
        (Market.btts(), Selection.YES, (1, 1), Settlement.WIN),
        (Market.btts(), Selection.NO, (2, 0), Settlement.WIN),
        (Market.totals(2.5), Selection.OVER, (2, 1), Settlement.WIN),
        (Market.totals(2.5), Selection.UNDER, (2, 1), Settlement.LOSS),
        (Market.totals(3.0), Selection.OVER, (2, 1), Settlement.PUSH),
        (Market.totals(2.25), Selection.OVER, (1, 1), Settlement.HALF_LOSS),
        (Market.totals(2.25), Selection.UNDER, (1, 1), Settlement.HALF_WIN),
        (Market.asian_handicap(-0.75), Selection.HOME, (1, 0), Settlement.HALF_WIN),
        (Market.asian_handicap(-0.75), Selection.AWAY, (1, 0), Settlement.HALF_LOSS),
        (Market.asian_handicap(-1.0), Selection.HOME, (2, 1), Settlement.PUSH),
        (Market.asian_handicap(0.5), Selection.AWAY, (1, 1), Settlement.LOSS),
    ],
)
def test_settle_selection(
    market: Market, selection: Selection, score: tuple[int, int], expected: Settlement
) -> None:
    assert settle_selection(market, selection, *score) is expected


def test_settle_selection_rejects_bad_input() -> None:
    with pytest.raises(ModelInputError):
        settle_selection(Market.btts(), Selection.YES, -1, 0)
    with pytest.raises(ValueError, match="not valid"):
        settle_selection(Market.btts(), Selection.OVER, 1, 0)


@given(
    st.integers(0, 8),
    st.integers(0, 8),
    st.sampled_from(ALL_MARKETS),
)
def test_settlement_agrees_with_model_on_a_certain_score(
    home_goals: int, away_goals: int, market: Market
) -> None:
    # A score matrix with all mass on one score must give probability 1 to its settlement.
    matrix = score_matrix(1.0, 1.0, max_goals=8) * 0.0
    matrix[home_goals, away_goals] = 1.0
    probabilities = market_from_matrix(matrix, market)
    for selection in market.selections:
        settlement = settle_selection(market, selection, home_goals, away_goals)
        assert probabilities.outcomes[selection].as_tuple()[settlement] == pytest.approx(1.0)
