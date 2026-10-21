"""Ensemble (F3.4): ``p = w * p_model + (1 - w) * p_market`` per selection.

The mix is applied to the full settlement distribution of each selection (win, half win,
push, half loss, loss), which is a convex combination and therefore again a distribution;
for win/lose markets this is exactly the per-selection formula. Two-sided Asian markets
mix the first side and derive the second as its mirror image, so both sides stay
consistent. Partition markets (1X2, BTTS, half lines) are renormalised so the win
probabilities sum to 1 exactly. ``w`` is chosen by the walk-forward backtest (ADR 0005).
"""

import math
from collections.abc import Mapping
from typing import Final

from app.domain import Market
from app.models.asian import AsianOutcomeProbabilities
from app.models.exceptions import ModelInputError
from app.models.market import FairMarket
from app.models.outcomes import (
    MarketProbabilities,
    binary_market,
    is_partition_market,
    two_sided_market,
)

# Starting point only: the backtest selects w on the validation season.
DEFAULT_MODEL_WEIGHT: Final = 0.5


def mix_outcomes(
    model: AsianOutcomeProbabilities, market: AsianOutcomeProbabilities, weight: float
) -> AsianOutcomeProbabilities:
    """``weight * model + (1 - weight) * market``, outcome by outcome."""
    _check_weight(weight)
    values = [
        weight * from_model + (1.0 - weight) * from_market
        for from_model, from_market in zip(model.as_tuple(), market.as_tuple(), strict=True)
    ]
    total = math.fsum(values)
    return AsianOutcomeProbabilities(*(value / total for value in values))


def ensemble(
    model: MarketProbabilities, market: MarketProbabilities, weight: float
) -> MarketProbabilities:
    """Mix the model's and the market's probabilities of one market.

    Raises:
        ModelInputError: the two describe different markets, or ``weight`` is not in [0, 1].
    """
    if model.market != market.market:
        msg = f"cannot mix {model.market} with {market.market}"
        raise ModelInputError(msg)
    _check_weight(weight)
    target = model.market
    mixed = {
        selection: mix_outcomes(model.outcomes[selection], market.outcomes[selection], weight)
        for selection in target.selections
    }
    if is_partition_market(target):
        total = math.fsum(outcome.win for outcome in mixed.values())
        return binary_market(
            target, {selection: outcome.win / total for selection, outcome in mixed.items()}
        )
    if target.line is not None:
        return two_sided_market(target, mixed[target.selections[0]])
    # Double chance: overlapping selections, each one is its own binary event.
    return MarketProbabilities(target, mixed)


def ensemble_markets(
    model: Mapping[Market, MarketProbabilities],
    market: Mapping[Market, FairMarket],
    weight: float,
) -> dict[Market, MarketProbabilities]:
    """Ensemble every market that has both a model and a fair market estimate.

    Markets without a market estimate are left out on purpose: a model-only number has no
    sanity anchor, and the confidence level treats a missing market as a weakness.
    """
    return {
        key: ensemble(probabilities, market[key].probabilities, weight)
        for key, probabilities in model.items()
        if key in market
    }


def _check_weight(weight: float) -> None:
    if not math.isfinite(weight) or not 0.0 <= weight <= 1.0:
        msg = f"ensemble weight must be in [0, 1], got {weight!r}"
        raise ModelInputError(msg)
