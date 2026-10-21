import math
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.models import (
    DEFAULT_KELLY_MULTIPLIER,
    DEFAULT_MAX_STAKE_FRACTION,
    AsianOutcomeProbabilities,
    ModelInputError,
    asian_kelly_fraction,
    capped_fraction,
    expected_value,
    flat_stake,
    kelly_fraction,
    stake_amount,
)

probabilities = st.floats(min_value=0.0, max_value=1.0)
prices = st.floats(min_value=1.01, max_value=50.0)


def test_defaults_match_the_plan() -> None:
    assert DEFAULT_KELLY_MULTIPLIER == 0.25
    assert DEFAULT_MAX_STAKE_FRACTION == 0.02


def test_kelly_hand_computed() -> None:
    # f* = (p * o - 1) / (o - 1)
    assert kelly_fraction(0.55, 2.0) == pytest.approx(0.10)
    assert kelly_fraction(0.40, 3.0) == pytest.approx(0.10)
    assert capped_fraction(kelly_fraction(0.52, 2.0)) == pytest.approx(0.01)
    assert capped_fraction(kelly_fraction(0.55, 2.0)) == pytest.approx(0.02)  # 0.025 capped
    assert capped_fraction(0.1, multiplier=0.5, max_fraction=1.0) == pytest.approx(0.05)


@given(probabilities, prices)
def test_kelly_is_zero_without_positive_ev(probability: float, odds: float) -> None:
    fraction = kelly_fraction(probability, odds)
    assert fraction >= 0.0
    if expected_value(probability, odds) <= 0.0:
        assert fraction == 0.0
        assert capped_fraction(fraction) == 0.0


@given(probabilities, prices)
def test_capped_fraction_is_bounded(probability: float, odds: float) -> None:
    fraction = capped_fraction(kelly_fraction(probability, odds))
    assert 0.0 <= fraction <= DEFAULT_MAX_STAKE_FRACTION


@given(probabilities, prices)
def test_asian_kelly_of_binary_bet_equals_closed_form(probability: float, odds: float) -> None:
    outcomes = AsianOutcomeProbabilities.binary(probability)
    assert asian_kelly_fraction(outcomes, odds) == pytest.approx(
        kelly_fraction(probability, odds), abs=1e-8
    )


def test_asian_kelly_hand_computed() -> None:
    # Returns +1 / -0.5 at odds 2: G'(f) = 0.5/(1+f) - 0.25/(1-0.5f) = 0  =>  f = 0.5.
    outcomes = AsianOutcomeProbabilities(0.5, 0.0, 0.0, 0.5, 0.0)
    assert asian_kelly_fraction(outcomes, 2.0) == pytest.approx(0.5, abs=1e-9)


def test_asian_kelly_never_exceeds_the_bankroll() -> None:
    # Only half-losses can hurt and growth still rises at f = 1: stake capped at everything.
    outcomes = AsianOutcomeProbabilities(0.9, 0.0, 0.0, 0.1, 0.0)
    assert asian_kelly_fraction(outcomes, 3.0) == 1.0
    riskless = AsianOutcomeProbabilities(0.0, 0.5, 0.5, 0.0, 0.0)
    assert asian_kelly_fraction(riskless, 1.5) == 1.0


def test_asian_kelly_is_zero_without_positive_ev() -> None:
    outcomes = AsianOutcomeProbabilities(0.3, 0.0, 0.2, 0.0, 0.5)
    assert asian_kelly_fraction(outcomes, 2.0) == 0.0


def test_stake_amount_rounds_down_to_cents() -> None:
    assert stake_amount(Decimal(1000), 0.02) == Decimal("20.00")
    assert stake_amount(Decimal("333.33"), 0.015) == Decimal("4.99")
    assert stake_amount(Decimal(0), 0.02) == Decimal("0.00")


def test_flat_stake_only_on_positive_ev() -> None:
    assert flat_stake(0.05, Decimal(10)) == Decimal(10)
    assert flat_stake(0.0, Decimal(10)) == Decimal(0)
    assert flat_stake(-0.2, Decimal(10)) == Decimal(0)


@pytest.mark.parametrize(
    "call",
    [
        lambda: capped_fraction(0.1, multiplier=0.0),
        lambda: capped_fraction(0.1, multiplier=1.5),
        lambda: capped_fraction(0.1, max_fraction=math.nan),
        lambda: capped_fraction(math.inf),
        lambda: stake_amount(Decimal(-1), 0.02),
        lambda: stake_amount(Decimal("NaN"), 0.02),
        lambda: stake_amount(Decimal(100), 1.5),
        lambda: stake_amount(Decimal(100), -0.01),
        lambda: flat_stake(0.1, Decimal(-1)),
        lambda: flat_stake(math.nan, Decimal(1)),
        lambda: kelly_fraction(0.5, 1.0),
    ],
)
def test_invalid_inputs_are_rejected(call: object) -> None:
    with pytest.raises(ModelInputError):
        call()  # type: ignore[operator]
