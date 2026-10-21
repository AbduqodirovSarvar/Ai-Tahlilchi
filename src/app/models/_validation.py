"""Input checks shared by the model modules (one definition of "valid odds/probability")."""

import math
from collections.abc import Sequence

from app.models.exceptions import ModelInputError

MIN_MARKET_OUTCOMES = 2


def check_odds(odds: float) -> float:
    """Return ``odds`` if it is a finite decimal price strictly above 1."""
    if not math.isfinite(odds) or odds <= 1.0:
        msg = f"decimal odds must be finite and > 1, got {odds!r}"
        raise ModelInputError(msg)
    return float(odds)


def check_market_odds(odds: Sequence[float]) -> tuple[float, ...]:
    """Validate the prices of one market: at least two outcomes, each valid."""
    if len(odds) < MIN_MARKET_OUTCOMES:
        msg = f"a market needs at least {MIN_MARKET_OUTCOMES} outcomes, got {len(odds)}"
        raise ModelInputError(msg)
    return tuple(check_odds(price) for price in odds)


def check_probability(probability: float) -> float:
    """Return ``probability`` if it is a finite number in [0, 1]."""
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        msg = f"probability must be in [0, 1], got {probability!r}"
        raise ModelInputError(msg)
    return float(probability)
