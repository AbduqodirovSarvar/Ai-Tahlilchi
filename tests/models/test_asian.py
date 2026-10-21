import math

import pytest

from app.models import (
    AsianOutcomeProbabilities,
    ModelInputError,
    Settlement,
    is_quarter_line,
    settle,
    split_line,
)
from app.models.asian import check_line, settlement_returns


@pytest.mark.parametrize(
    ("margin", "expected"),
    [
        (1.0, Settlement.WIN),
        (0.5, Settlement.WIN),
        (0.25, Settlement.HALF_WIN),  # e.g. over 2.75 with 3 goals
        (0.0, Settlement.PUSH),
        (-0.25, Settlement.HALF_LOSS),  # e.g. over 2.25 with 2 goals
        (-0.5, Settlement.LOSS),
        (-3.75, Settlement.LOSS),
    ],
)
def test_settle_by_margin(margin: float, expected: Settlement) -> None:
    assert settle(margin) is expected


@pytest.mark.parametrize(
    ("line", "halves", "quarter"),
    [
        (2.25, (2.0, 2.5), True),
        (2.75, (2.5, 3.0), True),
        (-0.75, (-1.0, -0.5), True),
        (2.5, (2.5, 2.5), False),
        (0.0, (0.0, 0.0), False),
        (-1.0, (-1.0, -1.0), False),
    ],
)
def test_split_line(line: float, halves: tuple[float, float], *, quarter: bool) -> None:
    assert split_line(line) == halves
    assert is_quarter_line(line) is quarter


def test_check_line_snaps_float_noise() -> None:
    assert check_line(0.1 + 0.15) == 0.25
    assert check_line(2.2500000000001) == 2.25


@pytest.mark.parametrize("line", [2.1, 0.3, math.nan, math.inf])
def test_invalid_lines_are_rejected(line: float) -> None:
    with pytest.raises(ModelInputError):
        check_line(line)


def test_outcome_probabilities_opposite_and_binary() -> None:
    outcomes = AsianOutcomeProbabilities(0.4, 0.1, 0.2, 0.25, 0.05)
    assert outcomes.opposite().as_tuple() == (0.05, 0.25, 0.2, 0.1, 0.4)
    assert outcomes.opposite().opposite() == outcomes
    assert AsianOutcomeProbabilities.binary(0.3).as_tuple() == (0.3, 0.0, 0.0, 0.0, 0.7)


@pytest.mark.parametrize(
    "values",
    [(0.5, 0.5, 0.5, 0.0, 0.0), (1.1, 0.0, 0.0, 0.0, -0.1), (math.nan, 0.0, 0.0, 0.0, 1.0)],
)
def test_invalid_outcome_probabilities_are_rejected(
    values: tuple[float, float, float, float, float],
) -> None:
    with pytest.raises(ModelInputError):
        AsianOutcomeProbabilities(*values)


def test_settlement_returns() -> None:
    assert settlement_returns(1.9) == pytest.approx((0.9, 0.45, 0.0, -0.5, -1.0))
    with pytest.raises(ModelInputError):
        settlement_returns(1.0)
