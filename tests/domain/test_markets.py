"""Tests for app.domain.markets."""

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain import (
    LINE_STEP,
    SELECTIONS_BY_MARKET,
    DomainValidationError,
    Market,
    MarketType,
    Selection,
    normalize_line,
)

quarter_steps = st.integers(min_value=-1000, max_value=1000)


@given(quarter_steps)
def test_lines_on_the_quarter_grid_are_accepted_exactly(steps: int) -> None:
    line = steps * LINE_STEP
    assert normalize_line(line) == line


@given(quarter_steps, st.floats(min_value=-1e-12, max_value=1e-12))
def test_float_noise_is_snapped_to_the_grid(steps: int, noise: float) -> None:
    assert normalize_line(steps * LINE_STEP + noise) == steps * LINE_STEP


@given(
    st.floats(min_value=-500, max_value=500, allow_nan=False).filter(
        lambda value: abs(value * 4 - round(value * 4)) > 1e-6
    )
)
def test_off_grid_lines_are_rejected(line: float) -> None:
    with pytest.raises(DomainValidationError, match=r"multiple of 0\.25"):
        normalize_line(line)


@pytest.mark.parametrize("line", [math.nan, math.inf, -math.inf])
def test_non_finite_lines_are_rejected(line: float) -> None:
    with pytest.raises(DomainValidationError, match="finite"):
        normalize_line(line)


def test_negative_zero_line_is_normalised() -> None:
    assert math.copysign(1.0, normalize_line(-0.0)) == 1.0


@pytest.mark.parametrize(
    "market_type", [MarketType.MATCH_RESULT, MarketType.DOUBLE_CHANCE, MarketType.BTTS]
)
def test_lineless_markets_reject_a_line(market_type: MarketType) -> None:
    with pytest.raises(DomainValidationError, match="has no line"):
        Market(market_type, 2.5)


@pytest.mark.parametrize("market_type", [MarketType.TOTALS, MarketType.ASIAN_HANDICAP])
def test_line_markets_require_a_line(market_type: MarketType) -> None:
    with pytest.raises(DomainValidationError, match="requires a line"):
        Market(market_type)


@pytest.mark.parametrize("line", [0.0, -0.5])
def test_totals_line_must_be_positive(line: float) -> None:
    with pytest.raises(DomainValidationError, match="positive"):
        Market.totals(line)


def test_asian_handicap_allows_zero_and_negative_quarter_lines() -> None:
    assert Market.asian_handicap(0).line == 0.0
    assert Market.asian_handicap(-0.75).line == -0.75


def test_constructors_build_the_expected_markets() -> None:
    assert Market.match_result() == Market(MarketType.MATCH_RESULT)
    assert Market.double_chance().type is MarketType.DOUBLE_CHANCE
    assert Market.btts().type is MarketType.BTTS
    assert Market.totals(2.5) == Market(MarketType.TOTALS, 2.5000000000001)


def test_markets_with_equal_lines_are_equal_and_hashable() -> None:
    assert {Market.totals(2.5), Market.totals(2.5), Market.totals(3.5)} == {
        Market.totals(2.5),
        Market.totals(3.5),
    }


def test_market_text_form() -> None:
    assert str(Market.match_result()) == "1x2"
    assert str(Market.totals(2.5)) == "totals 2.5"
    assert str(Market.asian_handicap(-1)) == "asian_handicap -1"


@pytest.mark.parametrize("market_type", list(MarketType))
def test_every_market_type_declares_its_selections(market_type: MarketType) -> None:
    assert SELECTIONS_BY_MARKET[market_type]


def test_require_selection_rejects_selection_of_another_market() -> None:
    market = Market.totals(2.5)
    assert market.require_selection(Selection.OVER) is Selection.OVER
    with pytest.raises(DomainValidationError, match="not valid"):
        market.require_selection(Selection.HOME)


def test_market_is_immutable() -> None:
    market = Market.totals(2.5)
    with pytest.raises(AttributeError):
        market.line = 3.5  # type: ignore[misc]
