"""Stake sizing: fractional Kelly with a hard cap, and flat staking.

Full Kelly maximises long-run log growth but only if the probabilities are exact; ours are
estimates, so a fraction (default 1/4) is staked and capped at a share of the bankroll
(default 2%). A bet without positive EV is never staked, and a stake is never negative.
"""

import math
from decimal import ROUND_DOWN, Decimal
from typing import Final

from scipy.optimize import brentq  # type: ignore[import-untyped]  # no scipy stubs in the lock (see report)

from app.models._validation import check_odds
from app.models.asian import AsianOutcomeProbabilities, settlement_returns
from app.models.ev import asian_expected_value, expected_value
from app.models.exceptions import ModelInputError

DEFAULT_KELLY_MULTIPLIER: Final = 0.25
DEFAULT_MAX_STAKE_FRACTION: Final = 0.02
MONEY_QUANTUM: Final = Decimal("0.01")
# Root finding for the Asian Kelly fraction: far below any meaningful stake difference.
_KELLY_XTOL: Final = 1e-12
# Stay this far inside the bankruptcy boundary, where log growth is -inf.
_BOUNDARY_MARGIN: Final = 1e-9


def kelly_fraction(probability: float, odds: float) -> float:
    """Full-Kelly share of the bankroll for a win/lose bet: ``EV / (odds - 1)``, at least 0."""
    edge = expected_value(probability, odds)
    return max(edge, 0.0) / (check_odds(odds) - 1.0)


def asian_kelly_fraction(outcomes: AsianOutcomeProbabilities, odds: float) -> float:
    """Full-Kelly share for an Asian bet, by maximising expected log growth numerically.

    Growth ``G(f) = sum(p_k * log(1 + f * r_k))`` is concave; its slope at 0 is the EV, so
    the optimum is 0 unless EV > 0. The result is limited to 1 (no leverage).
    """
    if asian_expected_value(outcomes, odds) <= 0.0:
        return 0.0
    pairs = [
        (p, r) for p, r in zip(outcomes.as_tuple(), settlement_returns(odds), strict=True) if p > 0
    ]
    worst_return = min(r for _, r in pairs)
    # 1 + f * r must stay positive for every possible outcome.
    upper = 1.0 if worst_return >= 0.0 else min(1.0, -1.0 / worst_return - _BOUNDARY_MARGIN)

    def growth_slope(fraction: float) -> float:
        return math.fsum(p * r / (1.0 + fraction * r) for p, r in pairs)

    if growth_slope(upper) >= 0.0:
        return upper
    return float(brentq(growth_slope, 0.0, upper, xtol=_KELLY_XTOL))


def capped_fraction(
    full_kelly: float,
    *,
    multiplier: float = DEFAULT_KELLY_MULTIPLIER,
    max_fraction: float = DEFAULT_MAX_STAKE_FRACTION,
) -> float:
    """Fractional Kelly: ``multiplier * full_kelly``, limited to ``[0, max_fraction]``."""
    for name, value in (("multiplier", multiplier), ("max_fraction", max_fraction)):
        if not math.isfinite(value) or not 0.0 < value <= 1.0:
            msg = f"{name} must be in (0, 1], got {value!r}"
            raise ModelInputError(msg)
    if not math.isfinite(full_kelly):
        msg = f"full_kelly must be finite, got {full_kelly!r}"
        raise ModelInputError(msg)
    return min(max(multiplier * full_kelly, 0.0), max_fraction)


def stake_amount(bankroll: Decimal, fraction: float) -> Decimal:
    """Money to stake: ``bankroll * fraction`` rounded *down* to whole cents."""
    if not bankroll.is_finite() or bankroll < 0:
        msg = f"bankroll must be finite and >= 0, got {bankroll!r}"
        raise ModelInputError(msg)
    if not math.isfinite(fraction) or not 0.0 <= fraction <= 1.0:
        msg = f"fraction must be in [0, 1], got {fraction!r}"
        raise ModelInputError(msg)
    return (bankroll * Decimal(repr(fraction))).quantize(MONEY_QUANTUM, rounding=ROUND_DOWN)


def flat_stake(ev: float, unit: Decimal) -> Decimal:
    """Flat staking: one ``unit`` on every bet with positive EV, nothing otherwise."""
    if not unit.is_finite() or unit < 0:
        msg = f"unit must be finite and >= 0, got {unit!r}"
        raise ModelInputError(msg)
    if not math.isfinite(ev):
        msg = f"ev must be finite, got {ev!r}"
        raise ModelInputError(msg)
    return unit if ev > 0.0 else Decimal(0)
