"""Expected value and fair odds of a bet.

EV is the expected profit per unit stake. For a plain win/lose bet ``EV = p * odds - 1``;
for Asian lines the half-win / push / half-loss outcomes contribute their partial profits.
This is the only place the EV formula is written.
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
