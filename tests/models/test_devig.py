import itertools
import math

import numpy as np
import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from app.models import (
    DevigMethod,
    ModelInputError,
    devig,
    devig_power,
    devig_proportional,
    devig_shin,
    implied_probabilities,
    overround,
    shin_insider_share,
)

odds_values = st.floats(min_value=1.01, max_value=50.0, allow_nan=False)
markets = st.lists(odds_values, min_size=2, max_size=4)
# A "true" distribution of 2-4 outcomes, none negligible.
true_distributions = st.lists(st.floats(min_value=0.02, max_value=1.0), min_size=2, max_size=4).map(
    lambda weights: [w / sum(weights) for w in weights]
)


def test_implied_probabilities_and_overround_hand_computed() -> None:
    assert implied_probabilities([2.0, 4.0, 5.0]) == pytest.approx((0.5, 0.25, 0.2))
    # 1/1.9 + 1/1.9 = 1.0526315789...
    assert overround([1.9, 1.9]) == pytest.approx(0.0526315789, abs=1e-9)


def test_proportional_hand_computed_three_way() -> None:
    # implied 0.4, 0.3125, 0.33333; sum 1.0458333
    assert devig_proportional([2.5, 3.2, 3.0]) == pytest.approx(
        (0.3824701195, 0.2988047809, 0.3187250996), abs=1e-9
    )


@given(st.floats(min_value=1.02, max_value=20.0), st.floats(min_value=1.02, max_value=20.0))
def test_shin_two_way_equals_additive_and_closed_form_z(odds_a: float, odds_b: float) -> None:
    # Known identities for two outcomes: Shin == additive de-vig, and z has a closed form.
    implied = np.array([1.0 / odds_a, 1.0 / odds_b])
    booksum = implied.sum()
    probabilities = devig_shin([odds_a, odds_b])
    if booksum <= 1.0:
        assert probabilities == pytest.approx(tuple(implied / booksum), abs=1e-9)
        return
    assert probabilities == pytest.approx(tuple(implied - (booksum - 1.0) / 2.0), abs=1e-9)
    if all(p > 0 for p in probabilities):
        diff = implied[0] - implied[1]
        z = (booksum - 1.0) * (diff**2 - booksum) / (booksum * (diff**2 - 1.0))
        assert shin_insider_share([odds_a, odds_b]) == pytest.approx(z, abs=1e-9)


@given(true_distributions, st.floats(min_value=0.0, max_value=0.15))
# Regression: z = 0 gives a booksum of 1 + 1e-16 whose rounding left the root unbracketed.
@example(truth=[0.7638821965624993] + [0.0787059344791669] * 3, insider_share=0.0)
def test_shin_recovers_true_probabilities_from_shin_prices(
    truth: list[float], insider_share: float
) -> None:
    # Forward Shin model: pi_i = beta * sqrt(z p_i + (1 - z) p_i^2), beta = sum of the roots.
    roots = [math.sqrt(insider_share * p + (1.0 - insider_share) * p * p) for p in truth]
    beta = sum(roots)
    odds = [1.0 / (beta * root) for root in roots]
    assert devig_shin(odds) == pytest.approx(tuple(truth), abs=1e-8)
    if insider_share > 1e-6:
        assert shin_insider_share(odds) == pytest.approx(insider_share, abs=1e-7)


@given(true_distributions, st.floats(min_value=1.0001, max_value=1.5))
def test_power_recovers_true_probabilities_from_power_prices(
    truth: list[float], exponent: float
) -> None:
    odds = [1.0 / p ** (1.0 / exponent) for p in truth]
    assert devig_power(odds) == pytest.approx(tuple(truth), abs=1e-9)


@pytest.mark.parametrize("method", list(DevigMethod))
@given(odds=markets)
def test_every_method_returns_a_distribution(method: DevigMethod, odds: list[float]) -> None:
    probabilities = devig(odds, method)
    assert len(probabilities) == len(odds)
    assert math.fsum(probabilities) == pytest.approx(1.0, abs=1e-9)
    assert all(0.0 < p < 1.0 for p in probabilities)
    # Shorter price, higher probability.
    order_by_odds = np.argsort(odds, kind="stable")
    assert all(
        probabilities[a] >= probabilities[b] - 1e-12 for a, b in itertools.pairwise(order_by_odds)
    )


@pytest.mark.parametrize("method", list(DevigMethod))
def test_fair_market_is_left_unchanged(method: DevigMethod) -> None:
    assert devig([2.0, 4.0, 4.0], method) == pytest.approx((0.5, 0.25, 0.25), abs=1e-12)


def test_shin_and_power_move_margin_onto_the_longshot() -> None:
    odds = [1.5, 4.2, 7.0]
    proportional = devig_proportional(odds)
    for shifted in (devig_shin(odds), devig_power(odds)):
        assert shifted[0] > proportional[0]
        assert shifted[2] < proportional[2]


def test_shin_without_margin_falls_back_to_proportional() -> None:
    odds = [2.1, 2.1]  # overround < 0: best prices across bookmakers
    assert shin_insider_share(odds) == 0.0
    assert devig_shin(odds) == pytest.approx(devig_proportional(odds))


def test_default_method_is_shin() -> None:
    odds = [2.3, 3.3, 3.4]
    assert devig(odds) == devig_shin(odds)


@pytest.mark.parametrize(
    "odds", [[2.0], [], [1.0, 2.0], [0.5, 3.0], [math.nan, 2.0], [math.inf, 2.0]]
)
@pytest.mark.parametrize("method", list(DevigMethod))
def test_invalid_markets_are_rejected(odds: list[float], method: DevigMethod) -> None:
    with pytest.raises(ModelInputError):
        devig(odds, method)
