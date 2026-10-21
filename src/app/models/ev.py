"""Expected value and fair odds of a bet.

EV is the expected profit per unit stake. For a plain win/lose bet ``EV = p * odds - 1``;
for Asian lines the half-win / push / half-loss outcomes contribute their partial profits.
This is the only place the EV and fair-odds formulas are written.
"""

import math

from app.models._validation import check_odds, check_probability
from app.models.asian import AsianOutcomeProbabilities, settlement_returns
from app.models.exceptions import ModelInputError


def expected_value(probability: float, odds: float) -> float:
    """EV per unit stake of a win/lose bet: ``p * odds - 1`` (0.05 means +5%)."""
    return check_probability(probability) * check_odds(odds) - 1.0


def asian_expected_value(outcomes: AsianOutcomeProbabilities, odds: float) -> float:
    """EV per unit stake of an Asian bet, weighting each settlement's profit.

    Equals :func:`expected_value` when ``outcomes`` is :meth:`AsianOutcomeProbabilities.binary`.
    """
    returns = settlement_returns(odds)
    return math.fsum(p * r for p, r in zip(outcomes.as_tuple(), returns, strict=True))


def fair_odds(probability: float) -> float:
    """Decimal odds with zero EV: ``1 / p``. Undefined for ``p = 0``."""
    if check_probability(probability) == 0.0:
        msg = "fair odds are undefined for probability 0"
        raise ModelInputError(msg)
    return 1.0 / probability


def asian_fair_odds(outcomes: AsianOutcomeProbabilities) -> float:
    """Decimal odds at which an Asian bet has zero EV.

    Solving ``(o - 1)(win + half_win/2) = loss + half_loss/2`` gives
    ``o = 1 + (loss + half_loss/2) / (win + half_win/2)``; pushes do not matter. Equals
    :func:`fair_odds` for a binary bet. Undefined when the bet cannot win anything.
    """
    gain = outcomes.win + outcomes.half_win / 2.0
    if gain <= 0.0:
        msg = f"fair odds are undefined for a bet that cannot win: {outcomes}"
        raise ModelInputError(msg)
    return 1.0 + (outcomes.loss + outcomes.half_loss / 2.0) / gain
