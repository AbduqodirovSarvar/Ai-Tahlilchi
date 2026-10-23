"""Express arithmetic: the rules of plan §6.11 as properties."""

import math
from dataclasses import replace
from datetime import timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain import Market, Selection
from app.services.errors import ValidationFailedError
from app.services.express_math import check_ids, evaluate
from app.services.interfaces import ResultMark, SignalSource, SignalState, SignalView
from tests.bot.samples import KICKOFF, NOW, make_signal

probabilities = st.floats(min_value=0.05, max_value=0.99)
odds_values = st.floats(min_value=1.05, max_value=10.0)


def legs(count: int, **overrides: object) -> list[SignalView]:
    return [
        make_signal(index, fixture_id=f"fx{index}", **overrides) for index in range(1, count + 1)
    ]


@given(st.lists(st.tuples(probabilities, odds_values), min_size=2, max_size=4))
def test_combined_numbers_are_the_products(pairs: list[tuple[float, float]]) -> None:
    views = [
        make_signal(i, fixture_id=f"fx{i}", probability=p, odds=o, ev=p * o - 1.0)
        for i, (p, o) in enumerate(pairs, start=1)
    ]

    express = evaluate(views, NOW)

    probability = math.prod(p for p, _ in pairs)
    assert express.combined_probability == pytest.approx(probability)
    assert express.combined_probability <= min(p for p, _ in pairs) + 1e-12
    assert express.combined_odds == pytest.approx(math.prod(o for _, o in pairs))
    assert express.ev == pytest.approx(probability * express.combined_odds - 1)


@given(st.lists(st.floats(min_value=-0.5, max_value=0.5), min_size=2, max_size=4))
def test_express_ev_compounds_the_legs_own_ev(evs: list[float]) -> None:
    """Quarter-line Asian legs: p * odds - 1 is not the leg's EV, prod(1 + ev) - 1 is."""
    views = [
        make_signal(i, fixture_id=f"fx{i}", probability=0.5, odds=2.0, ev=ev)
        for i, ev in enumerate(evs, start=1)
    ]

    express = evaluate(views, NOW)

    assert express.ev == pytest.approx(math.prod(1.0 + ev for ev in evs) - 1.0)


def test_express_fair_odds_multiply_the_legs_fair_odds() -> None:
    views = [
        make_signal(1, fixture_id="fx1", probability=0.5, odds=2.0, ev=-0.1, zero_ev_odds=2.2),
        make_signal(2, fixture_id="fx2", probability=0.5, odds=2.0, ev=0.0, zero_ev_odds=2.0),
    ]

    express = evaluate(views, NOW)

    assert express.fair_odds == pytest.approx(2.2 * 2.0)


def test_three_seventy_percent_picks_make_a_34_percent_express() -> None:
    express = evaluate(legs(3, probability=0.7), NOW)

    assert round(express.combined_probability * 100) == 34


@pytest.mark.parametrize("ids", [[], [1], [1, 2, 3, 4, 5], [1, 1], [1, 2, 2]])
def test_leg_count_and_repeats_are_refused(ids: list[int]) -> None:
    with pytest.raises(ValidationFailedError):
        check_ids(ids)


def test_two_picks_of_one_match_are_refused() -> None:
    same = [make_signal(1), make_signal(2, market=Market.btts(), selection=Selection.YES)]

    with pytest.raises(ValidationFailedError, match="correlated"):
        evaluate(same, NOW)


@pytest.mark.parametrize(
    "change",
    [
        {"source": SignalSource.RETRO, "recommended_at": None},
        {"recommended_at": None, "no_value": True},
        {"state": SignalState.CANCELLED},
        {"result": ResultMark.WON},
    ],
)
def test_picks_that_cannot_be_played_are_refused(change: dict[str, object]) -> None:
    views = legs(2)
    views[1] = replace(views[1], **change)  # type: ignore[arg-type]

    with pytest.raises(ValidationFailedError):
        evaluate(views, NOW)


def test_a_started_match_is_refused() -> None:
    with pytest.raises(ValidationFailedError, match="no longer"):
        evaluate(legs(2), KICKOFF + timedelta(minutes=1))
