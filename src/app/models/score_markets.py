"""Betting-market probabilities derived from a score probability matrix.

Every function takes ``matrix[home_goals, away_goals]`` (e.g. from
:func:`app.models.poisson.score_matrix`), which must be a valid distribution, and returns
probabilities of one complete market, so the outcomes of every market sum to 1.
"""

import math
from dataclasses import dataclass
from typing import Final

import numpy as np
import numpy.typing as npt

from app.models.asian import AsianOutcomeProbabilities, Settlement, check_line, settle
from app.models.exceptions import ModelInputError

type FloatArray = npt.NDArray[np.float64]

# The matrix may deviate from a distribution by at most this much (float accumulation).
MATRIX_SUM_TOLERANCE: Final = 1e-9
_MATRIX_DIMENSIONS: Final = 2
DEFAULT_TOP_SCORES: Final = 5


@dataclass(frozen=True, slots=True)
class MatchResultProbabilities:
    """1X2: home win, draw, away win."""

    home: float
    draw: float
    away: float


@dataclass(frozen=True, slots=True)
class DoubleChanceProbabilities:
    """Double chance: 1X, 12, X2. Each is a two-way market with its complement."""

    home_or_draw: float
    home_or_away: float
    draw_or_away: float


@dataclass(frozen=True, slots=True)
class BothTeamsToScoreProbabilities:
    """BTTS yes / no."""

    yes: float
    no: float


@dataclass(frozen=True, slots=True)
class TwoSidedAsianProbabilities:
    """Both sides of one Asian line: over/under a total, or home/away on a handicap."""

    line: float
    first: AsianOutcomeProbabilities
    second: AsianOutcomeProbabilities


@dataclass(frozen=True, slots=True)
class ScoreProbability:
    """Probability of one exact score."""

    home_goals: int
    away_goals: int
    probability: float


def match_result(matrix: FloatArray) -> MatchResultProbabilities:
    """1X2 probabilities: below, on and above the matrix diagonal."""
    checked = _check_matrix(matrix)
    return MatchResultProbabilities(
        home=float(np.tril(checked, -1).sum()),
        draw=float(np.trace(checked)),
        away=float(np.triu(checked, 1).sum()),
    )


def double_chance(matrix: FloatArray) -> DoubleChanceProbabilities:
    """Double-chance probabilities from the 1X2 split."""
    result = match_result(matrix)
    return DoubleChanceProbabilities(
        home_or_draw=result.home + result.draw,
        home_or_away=result.home + result.away,
        draw_or_away=result.draw + result.away,
    )


def both_teams_to_score(matrix: FloatArray) -> BothTeamsToScoreProbabilities:
    """BTTS: both teams score at least once."""
    checked = _check_matrix(matrix)
    no = float(checked[0, :].sum() + checked[1:, 0].sum())
    return BothTeamsToScoreProbabilities(yes=1.0 - no, no=no)


def total_goals(matrix: FloatArray, line: float) -> TwoSidedAsianProbabilities:
    """Over (``first``) / under (``second``) any total line, incl. quarter lines like 2.25.

    Half lines (2.5) only win or lose; whole lines (2.0) can push; quarter lines can
    half-win or half-lose.
    """
    snapped = check_line(line)
    if snapped < 0.0:
        msg = f"a total line cannot be negative, got {line!r}"
        raise ModelInputError(msg)
    checked = _check_matrix(matrix)
    grid = np.indices(checked.shape)
    over = _settle_distribution(grid[0] + grid[1], checked, -snapped)
    return TwoSidedAsianProbabilities(line=snapped, first=over, second=over.opposite())


def asian_handicap(matrix: FloatArray, home_line: float) -> TwoSidedAsianProbabilities:
    """Asian handicap: home (``first``) with ``home_line``, away with ``-home_line``.

    ``home_line = -0.75`` means the home team gives 0.75 goals: half the stake on -0.5 and
    half on -1.0.
    """
    snapped = check_line(home_line)
    checked = _check_matrix(matrix)
    grid = np.indices(checked.shape)
    home = _settle_distribution(grid[0] - grid[1], checked, snapped)
    return TwoSidedAsianProbabilities(line=snapped, first=home, second=home.opposite())


def top_scores(matrix: FloatArray, count: int = DEFAULT_TOP_SCORES) -> tuple[ScoreProbability, ...]:
    """The ``count`` most likely exact scores, most likely first (ties: fewer goals first)."""
    checked = _check_matrix(matrix)
    if count < 1:
        msg = f"count must be >= 1, got {count!r}"
        raise ModelInputError(msg)
    home_goals, away_goals = (axis.ravel() for axis in np.indices(checked.shape))
    probabilities = checked.ravel()
    # lexsort sorts by the last key first: probability descending, then home, then away.
    order = np.lexsort((away_goals, home_goals, -probabilities))[:count]
    return tuple(
        ScoreProbability(
            home_goals=int(home_goals[i]),
            away_goals=int(away_goals[i]),
            probability=float(probabilities[i]),
        )
        for i in order
    )


def _settle_distribution(
    statistic: npt.NDArray[np.int_], matrix: FloatArray, offset: float
) -> AsianOutcomeProbabilities:
    """Settlement probabilities of a bet whose margin is ``statistic + offset``."""
    values = statistic.ravel()
    lowest = int(values.min())
    # Probability of each integer value of the statistic (goal total or difference).
    mass = np.bincount(values - lowest, weights=matrix.ravel())
    totals = dict.fromkeys(Settlement, 0.0)
    for shift, probability in enumerate(mass.tolist()):
        totals[settle(lowest + shift + offset)] += probability
    # Clamp the ~1e-17 rounding of a complement and renormalise to an exact distribution.
    clean = [max(totals[outcome], 0.0) for outcome in Settlement]
    norm = math.fsum(clean)
    return AsianOutcomeProbabilities(*(value / norm for value in clean))


def _check_matrix(matrix: FloatArray) -> FloatArray:
    checked = np.asarray(matrix, dtype=np.float64)
    is_square = checked.ndim == _MATRIX_DIMENSIONS and checked.shape[0] == checked.shape[1]
    if not is_square or checked.size == 0:
        msg = f"score matrix must be a non-empty square 2-D array, got shape {checked.shape}"
        raise ModelInputError(msg)
    if not np.all(np.isfinite(checked)) or np.any(checked < 0.0):
        msg = "score matrix entries must be finite and >= 0"
        raise ModelInputError(msg)
    if abs(float(checked.sum()) - 1.0) > MATRIX_SUM_TOLERANCE:
        msg = f"score matrix must sum to 1, got {float(checked.sum())!r}"
        raise ModelInputError(msg)
    # Remove the tolerated float drift so derived markets sum to 1 to machine precision.
    return checked / checked.sum()
