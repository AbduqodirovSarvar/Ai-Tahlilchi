import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.models import (
    AsianOutcomeProbabilities,
    ModelInputError,
    asian_expected_value,
    asian_fair_odds,
    expected_value,
    fair_odds,
)

probabilities = st.floats(min_value=0.0, max_value=1.0)
prices = st.floats(min_value=1.01, max_value=100.0)


def test_expected_value_hand_computed() -> None:
    assert expected_value(0.55, 2.0) == pytest.approx(0.10)
    assert expected_value(0.5, 2.0) == pytest.approx(0.0)
    assert expected_value(1 / 1.07 * 0.95, 1.07) == pytest.approx(-0.05)


def test_fair_odds() -> None:
    assert fair_odds(0.25) == pytest.approx(4.0)
    assert expected_value(0.4, fair_odds(0.4)) == pytest.approx(0.0, abs=1e-15)
    with pytest.raises(ModelInputError):
        fair_odds(0.0)


def test_asian_expected_value_hand_computed() -> None:
    # Home -0.25 at 1.9: win 0.45, half-loss (draw) 0.27, loss 0.28.
    outcomes = AsianOutcomeProbabilities(0.45, 0.0, 0.0, 0.27, 0.28)
    assert asian_expected_value(outcomes, 1.9) == pytest.approx(0.45 * 0.9 - 0.135 - 0.28)
    # Over 2.75 at 2.0: win 0.35, half-win 0.2, loss 0.45.
    outcomes = AsianOutcomeProbabilities(0.35, 0.2, 0.0, 0.0, 0.45)
    assert asian_expected_value(outcomes, 2.0) == pytest.approx(0.35 + 0.1 - 0.45)


@given(probabilities, prices)
def test_asian_ev_of_binary_bet_equals_plain_ev(probability: float, odds: float) -> None:
    outcomes = AsianOutcomeProbabilities.binary(probability)
    assert asian_expected_value(outcomes, odds) == pytest.approx(
        expected_value(probability, odds), abs=1e-12
    )


@given(probabilities, probabilities, prices)
def test_ev_is_monotone_in_probability(p1: float, p2: float, odds: float) -> None:
    low, high = sorted((p1, p2))
    assert expected_value(low, odds) <= expected_value(high, odds)


@given(probabilities, prices, prices)
def test_ev_is_monotone_in_odds(probability: float, o1: float, o2: float) -> None:
    low, high = sorted((o1, o2))
    assert expected_value(probability, low) <= expected_value(probability, high)


@pytest.mark.parametrize(
    ("probability", "odds"),
    [(-0.1, 2.0), (1.1, 2.0), (math.nan, 2.0), (0.5, 1.0), (0.5, math.inf)],
)
def test_invalid_inputs_are_rejected(probability: float, odds: float) -> None:
    with pytest.raises(ModelInputError):
        expected_value(probability, odds)


def test_asian_fair_odds_zeroes_ev() -> None:
    outcomes = AsianOutcomeProbabilities(0.4, 0.1, 0.1, 0.15, 0.25)
    odds = asian_fair_odds(outcomes)
    assert odds == pytest.approx(1 + (0.25 + 0.075) / (0.4 + 0.05))
    assert asian_expected_value(outcomes, odds) == pytest.approx(0.0, abs=1e-12)
    with pytest.raises(ModelInputError, match="cannot win"):
        asian_fair_odds(AsianOutcomeProbabilities(0.0, 0.0, 0.5, 0.0, 0.5))


@given(st.floats(min_value=0.01, max_value=1.0))
def test_asian_fair_odds_of_binary_bet_is_one_over_p(probability: float) -> None:
    binary = AsianOutcomeProbabilities.binary(probability)
    assert asian_fair_odds(binary) == pytest.approx(fair_odds(probability))
