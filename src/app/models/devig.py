"""Remove the bookmaker margin from a market's prices ("de-vig").

A bookmaker's implied probabilities ``1/odds`` sum to more than 1; the excess is the margin
(overround). Every method here maps the prices of one *complete* market (all mutually
exclusive outcomes, e.g. 1X2 or over/under of one line) to probabilities that sum to 1.

- proportional (multiplicative): divide each implied probability by their sum. Simple, but
  spreads the margin evenly, which overstates longshots (favourite-longshot bias).
- Shin (1993): models a share ``z`` of insider money; the bookmaker loads the margin onto
  longshots. Solved numerically for ``z``. Preferred default for 3-way markets.
- power: ``p_i = pi_i ** k`` with ``k`` chosen so the sum is 1; also shifts margin to longshots.

Mathematical background and trade-offs: docs/adr/0003-dixon-coles-and-devig.md.
"""

import math
from collections.abc import Sequence
from enum import StrEnum
from typing import Final

import numpy as np
import numpy.typing as npt
from scipy.optimize import brentq  # type: ignore[import-untyped]  # no scipy stubs in the lock (see report)

from app.models._validation import check_market_odds

type FloatArray = npt.NDArray[np.float64]

# Root finding: ``xtol`` well below any meaningful probability difference.
_ROOT_XTOL: Final = 1e-14
# Shin's z lies in [0, 1); the open upper end is approached but never evaluated.
_SHIN_Z_UPPER: Final = 1.0 - 1e-12
# Power method: lower end of the exponent bracket (the sum is ~n > 1 there).
_POWER_K_LOWER: Final = 1e-9


class DevigMethod(StrEnum):
    """Available de-vig methods."""

    PROPORTIONAL = "proportional"
    SHIN = "shin"
    POWER = "power"


def implied_probabilities(odds: Sequence[float]) -> tuple[float, ...]:
    """Raw bookmaker probabilities ``1/odds`` (margin still included)."""
    return tuple(1.0 / price for price in check_market_odds(odds))


def overround(odds: Sequence[float]) -> float:
    """Bookmaker margin of a complete market: ``sum(1/odds) - 1`` (0.05 means 5%).

    Negative when the prices are better than fair, e.g. best prices across bookmakers.
    """
    return math.fsum(implied_probabilities(odds)) - 1.0


def devig_proportional(odds: Sequence[float]) -> tuple[float, ...]:
    """De-vig by scaling implied probabilities so they sum to 1."""
    implied = np.asarray(implied_probabilities(odds))
    return _as_tuple(implied / implied.sum())


def shin_insider_share(odds: Sequence[float]) -> float:
    """Shin's ``z``: the estimated share of insider trading implied by the margin.

    Zero when the market carries no margin (overround <= 0): Shin's model has no
    interpretation for negative margins, so those markets are left to proportional scaling.
    """
    return _solve_shin(np.asarray(implied_probabilities(odds)))[1]


def devig_shin(odds: Sequence[float]) -> tuple[float, ...]:
    """De-vig by Shin's method.

    For a market without margin (overround <= 0) this falls back to proportional scaling,
    see :func:`shin_insider_share`. For two-outcome markets it coincides with the additive
    method ``p_i = pi_i - margin / 2`` (a known identity, used as a test oracle).
    """
    return _as_tuple(_solve_shin(np.asarray(implied_probabilities(odds)))[0])


def devig_power(odds: Sequence[float]) -> tuple[float, ...]:
    """De-vig by the power method: ``p_i = pi_i ** k`` with ``sum(p_i) = 1``."""
    log_implied = np.log(np.asarray(implied_probabilities(odds)))

    def log_total(exponent: float) -> float:
        # log(sum(pi_i ** k)) computed stably in log space; strictly decreasing in k.
        return float(np.logaddexp.reduce(exponent * log_implied))

    # log(sum) <= log(n) + k * max(log pi_i) and max(log pi_i) < 0 (all odds > 1), so the
    # sum is certainly below 1 at twice the k where that bound crosses zero.
    upper = 2.0 * math.log(log_implied.size) / -float(log_implied.max()) + _POWER_K_LOWER
    exponent = brentq(log_total, _POWER_K_LOWER, upper, xtol=_ROOT_XTOL)
    probabilities = np.exp(exponent * log_implied)
    return _as_tuple(probabilities / probabilities.sum())


def devig(odds: Sequence[float], method: DevigMethod = DevigMethod.SHIN) -> tuple[float, ...]:
    """De-vig a complete market with the chosen method (Shin by default)."""
    match method:
        case DevigMethod.PROPORTIONAL:
            return devig_proportional(odds)
        case DevigMethod.SHIN:
            return devig_shin(odds)
        case DevigMethod.POWER:
            return devig_power(odds)


def _shin_probabilities(z: float, implied: FloatArray, booksum: float) -> FloatArray:
    """Shin's fair probabilities for a given insider share ``z`` (closed form per outcome)."""
    root = np.sqrt(z * z + 4.0 * (1.0 - z) * implied * implied / booksum)
    return np.asarray((root - z) / (2.0 * (1.0 - z)), dtype=np.float64)


def _solve_shin(implied: FloatArray) -> tuple[FloatArray, float]:
    """Find ``z`` with ``sum(p_i(z)) = 1`` and return ``(probabilities, z)``.

    ``sum(p_i(0)) = sqrt(booksum) > 1`` and ``sum(p_i(z -> 1)) = sum(pi_i^2) / booksum < 1``
    (every ``pi_i < 1``), so a root is always bracketed by ``[0, 1)`` when booksum > 1.
    """
    booksum = float(implied.sum())

    def excess(z: float) -> float:
        return float(_shin_probabilities(z, implied, booksum).sum()) - 1.0

    # Also catches a margin of ~1e-16, where rounding can make the excess at 0 non-positive
    # and leave the root unbracketed.
    if booksum <= 1.0 or excess(0.0) <= 0.0:
        return implied / booksum, 0.0
    z = float(brentq(excess, 0.0, _SHIN_Z_UPPER, xtol=_ROOT_XTOL))
    probabilities = _shin_probabilities(z, implied, booksum)
    # Remove the last ~1e-15 of root-finding error so the market sums to 1 exactly.
    return probabilities / probabilities.sum(), z


def _as_tuple(values: FloatArray) -> tuple[float, ...]:
    return tuple(float(value) for value in values)
