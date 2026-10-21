"""Settlement of Asian lines (handicaps and totals), including quarter lines.

A bet is settled on its *margin*: ``goal difference + handicap`` for an Asian handicap,
``total - line`` for an over and ``line - total`` for an under. Lines are multiples of 0.25:

- whole/half line (``margin`` a multiple of 0.5): one bet; > 0 wins, = 0 is a push, < 0 loses;
- quarter line (e.g. 2.25): the stake is split in half over the two neighbouring lines
  (2.0 and 2.5), which yields half-wins and half-losses.

Every market built on an Asian line is described by the probabilities of the five
settlement outcomes; EV and Kelly are computed from them (``app.models.ev``,
``app.models.staking``).
"""

import math
from dataclasses import dataclass
from enum import IntEnum
from typing import Final

from app.models._validation import check_odds
from app.models.exceptions import ModelInputError

QUARTER: Final = 0.25
_QUARTERS_PER_UNIT: Final = 4
_HALF_UNIT_QUARTERS: Final = 2
# A line closer than this to a multiple of 0.25 is treated as that multiple (float noise).
_LINE_TOLERANCE: Final = 1e-9
# Probabilities of a distribution must sum to 1 within this tolerance.
PROBABILITY_SUM_TOLERANCE: Final = 1e-9


class Settlement(IntEnum):
    """Settlement outcome of one Asian bet."""

    WIN = 0
    HALF_WIN = 1
    PUSH = 2
    HALF_LOSS = 3
    LOSS = 4


@dataclass(frozen=True, slots=True)
class AsianOutcomeProbabilities:
    """Probabilities of the five settlement outcomes of one Asian bet (sum to 1).

    A plain win/lose bet (1X2, BTTS, half-line totals) is the special case with only
    ``win`` and ``loss`` non-zero, see :meth:`binary`.
    """

    win: float
    half_win: float
    push: float
    half_loss: float
    loss: float

    def __post_init__(self) -> None:
        """Reject negative entries or a distribution that does not sum to 1."""
        values = self.as_tuple()
        if any(not math.isfinite(value) or value < 0.0 for value in values):
            msg = f"settlement probabilities must be finite and >= 0, got {values}"
            raise ModelInputError(msg)
        if abs(math.fsum(values) - 1.0) > PROBABILITY_SUM_TOLERANCE:
            msg = f"settlement probabilities must sum to 1, got {math.fsum(values)!r}"
            raise ModelInputError(msg)

    @classmethod
    def binary(cls, win_probability: float) -> "AsianOutcomeProbabilities":
        """A bet that either wins or loses in full."""
        return cls(win_probability, 0.0, 0.0, 0.0, 1.0 - win_probability)

    def as_tuple(self) -> tuple[float, float, float, float, float]:
        """Probabilities in :class:`Settlement` order."""
        return (self.win, self.half_win, self.push, self.half_loss, self.loss)

    def opposite(self) -> "AsianOutcomeProbabilities":
        """The same event seen from the other side (over vs under, home vs away line)."""
        return AsianOutcomeProbabilities(
            self.loss, self.half_loss, self.push, self.half_win, self.win
        )


def check_line(line: float) -> float:
    """Return ``line`` snapped to the nearest multiple of 0.25, or raise if it is not one."""
    quarters = round(line * _QUARTERS_PER_UNIT) if math.isfinite(line) else None
    if quarters is None or abs(line * _QUARTERS_PER_UNIT - quarters) > _LINE_TOLERANCE:
        msg = f"an Asian line must be a finite multiple of 0.25, got {line!r}"
        raise ModelInputError(msg)
    return quarters / _QUARTERS_PER_UNIT


def is_quarter_line(line: float) -> bool:
    """True for split-stake lines such as 2.25 or -0.75."""
    return round(check_line(line) * _QUARTERS_PER_UNIT) % _HALF_UNIT_QUARTERS != 0


def split_line(line: float) -> tuple[float, float]:
    """Decompose a line into the two half-stake lines it is settled as.

    A whole or half line is returned twice, so callers can always settle two halves.
    """
    snapped = check_line(line)
    if not is_quarter_line(snapped):
        return snapped, snapped
    return snapped - QUARTER, snapped + QUARTER


def settle(margin: float) -> Settlement:
    """Settle one bet from its margin (a multiple of 0.25, e.g. ``goals - 2.25``)."""
    quarters = round(check_line(margin) * _QUARTERS_PER_UNIT)
    if quarters >= _HALF_UNIT_QUARTERS:  # Margin >= 0.5: every half stake wins.
        return Settlement.WIN
    if quarters <= -_HALF_UNIT_QUARTERS:
        return Settlement.LOSS
    by_quarters = {
        1: Settlement.HALF_WIN,  # halves at margin 0 (push) and 0.5 (win)
        0: Settlement.PUSH,
        -1: Settlement.HALF_LOSS,  # halves at margin -0.5 (loss) and 0 (push)
    }
    return by_quarters[quarters]


def settlement_returns(odds: float) -> tuple[float, float, float, float, float]:
    """Profit per unit stake for each :class:`Settlement`, in enum order."""
    profit = check_odds(odds) - 1.0
    return (profit, profit / 2.0, 0.0, -0.5, -1.0)
