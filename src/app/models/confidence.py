"""Confidence level (F3.6): how much the probability of a signal can be trusted.

Probability and confidence are different things (main plan §6.6): a 60% estimate can be
well founded (long history, model and market agree, kickoff tomorrow) or shaky. The level
starts at HIGH and every weakness found lowers it by one step, down to LOW:

- thin data: fewer than ``GOOD_TEAM_MATCHES`` matches for the weaker-covered team;
- limited information: neither xG nor confirmed lineups are available;
- weak market anchor: no market estimate, or one from a fallback bookmaker;
- moderate model-market disagreement: ``|p_model - p_market| > MODERATE_DISAGREEMENT``
  (strict: a gap of exactly 8 p.p. does not count; the gap is rounded to 1e-9 first);
- calibration history: the bucket's past bias exceeds ``MAX_CALIBRATION_BIAS``;
- far from kickoff: more than ``PRELIMINARY_DAYS`` ahead (prices and news still move).

Two conditions force LOW outright: fewer than ``MIN_TEAM_MATCHES`` matches (the pipeline
skips such fixtures anyway), and a disagreement strictly above ``SANITY_DISAGREEMENT``
(exactly 15 p.p. is MEDIUM-level disagreement, not a sanity case), which also
raises the sanity flag: per the senior rules a gap that large is treated as a probable
bug or data error before it is ever treated as value.

HIGH needs xG *or* confirmed lineups. football-data history has no xG, so until lineups are
published (about an hour before kickoff) the best reachable level is MEDIUM.

The service layer has its own ``ConfidenceLevel`` with the same member names; the pipeline
maps by name (models do not import services).
"""

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.models._validation import check_probability
from app.models.exceptions import ModelInputError

MIN_TEAM_MATCHES: Final = 6
GOOD_TEAM_MATCHES: Final = 15
MODERATE_DISAGREEMENT: Final = 0.08
SANITY_DISAGREEMENT: Final = 0.15
PRELIMINARY_DAYS: Final = 3.0
MAX_CALIBRATION_BIAS: Final = 0.05
# The gap is rounded to 1e-9 before the (strict) comparisons, so an exact 15 p.p. gap such as
# 0.50 - 0.35 (= 0.15000000000000002 in floats) is judged like 0.25 - 0.10.
_DISAGREEMENT_DECIMALS: Final = 9


class ConfidenceLevel(StrEnum):
    """Confidence in a signal's probability, best first."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


_LEVELS: Final = (ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM, ConfidenceLevel.LOW)


class ConfidenceReason(StrEnum):
    """Why a level was lowered (stable codes; user-facing text lives in the bot)."""

    INSUFFICIENT_DATA = "insufficient_data"
    THIN_DATA = "thin_data"
    LIMITED_INFORMATION = "limited_information"
    NO_MARKET_REFERENCE = "no_market_reference"
    WIDE_MARKET_UNCERTAINTY = "wide_market_uncertainty"
    MODEL_MARKET_DISAGREEMENT = "model_market_disagreement"
    SANITY_DISAGREEMENT = "sanity_disagreement"
    CALIBRATION_BIAS = "calibration_bias"
    FAR_FROM_KICKOFF = "far_from_kickoff"


_FORCES_LOW: Final = frozenset(
    {ConfidenceReason.INSUFFICIENT_DATA, ConfidenceReason.SANITY_DISAGREEMENT}
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ConfidenceInputs:
    """Everything the confidence level depends on, for one selection.

    ``min_team_matches`` is the smaller of the two teams' match counts in the fit window.
    ``calibration_bias`` is the historical ``mean(predicted - observed)`` of the
    probability bucket this selection falls in (``None`` while there is no history).
    ``days_to_kickoff`` may be negative for retro analysis.
    """

    min_team_matches: int
    xg_available: bool
    lineups_known: bool
    model_probability: float
    market_probability: float | None
    days_to_kickoff: float
    market_wide_uncertainty: bool = False
    calibration_bias: float | None = None

    def __post_init__(self) -> None:
        """Reject impossible values instead of silently producing a level."""
        if self.min_team_matches < 0:
            msg = f"min_team_matches must be >= 0, got {self.min_team_matches}"
            raise ModelInputError(msg)
        check_probability(self.model_probability)
        if self.market_probability is not None:
            check_probability(self.market_probability)
        if not math.isfinite(self.days_to_kickoff):
            msg = f"days_to_kickoff must be finite, got {self.days_to_kickoff!r}"
            raise ModelInputError(msg)
        bias = self.calibration_bias
        if bias is not None and (not math.isfinite(bias) or abs(bias) > 1.0):
            msg = f"calibration_bias must be in [-1, 1], got {bias!r}"
            raise ModelInputError(msg)


@dataclass(frozen=True, slots=True)
class ConfidenceAssessment:
    """The level, every reason it was lowered, and the sanity flag."""

    level: ConfidenceLevel
    reasons: tuple[ConfidenceReason, ...]
    sanity_flag: bool
    disagreement: float | None


def assess_confidence(inputs: ConfidenceInputs) -> ConfidenceAssessment:
    """Confidence level of one selection (rules in the module docstring)."""
    disagreement = (
        None
        if inputs.market_probability is None
        else round(
            abs(inputs.model_probability - inputs.market_probability), _DISAGREEMENT_DECIMALS
        )
    )
    reasons = (
        *_data_reasons(inputs),
        *_market_reasons(inputs, disagreement),
        *_history_and_timing_reasons(inputs),
    )
    if _FORCES_LOW & set(reasons):
        level = ConfidenceLevel.LOW
    else:
        level = _LEVELS[min(len(reasons), len(_LEVELS) - 1)]
    return ConfidenceAssessment(
        level=level,
        reasons=reasons,
        sanity_flag=ConfidenceReason.SANITY_DISAGREEMENT in reasons,
        disagreement=disagreement,
    )


def lower_level(level: ConfidenceLevel, steps: int = 1) -> ConfidenceLevel:
    """``level`` lowered by ``steps`` (never below LOW)."""
    if steps < 0:
        msg = f"steps must be >= 0, got {steps}"
        raise ModelInputError(msg)
    return _LEVELS[min(_LEVELS.index(level) + steps, len(_LEVELS) - 1)]


def _data_reasons(inputs: ConfidenceInputs) -> tuple[ConfidenceReason, ...]:
    reasons = []
    if inputs.min_team_matches < MIN_TEAM_MATCHES:
        reasons.append(ConfidenceReason.INSUFFICIENT_DATA)
    elif inputs.min_team_matches < GOOD_TEAM_MATCHES:
        reasons.append(ConfidenceReason.THIN_DATA)
    if not inputs.xg_available and not inputs.lineups_known:
        reasons.append(ConfidenceReason.LIMITED_INFORMATION)
    return tuple(reasons)


def _market_reasons(
    inputs: ConfidenceInputs, disagreement: float | None
) -> tuple[ConfidenceReason, ...]:
    if disagreement is None:
        return (ConfidenceReason.NO_MARKET_REFERENCE,)
    reasons = []
    if inputs.market_wide_uncertainty:
        reasons.append(ConfidenceReason.WIDE_MARKET_UNCERTAINTY)
    if disagreement > SANITY_DISAGREEMENT:
        reasons.append(ConfidenceReason.SANITY_DISAGREEMENT)
    elif disagreement > MODERATE_DISAGREEMENT:
        reasons.append(ConfidenceReason.MODEL_MARKET_DISAGREEMENT)
    return tuple(reasons)


def _history_and_timing_reasons(inputs: ConfidenceInputs) -> tuple[ConfidenceReason, ...]:
    reasons = []
    bias = inputs.calibration_bias
    if bias is not None and abs(bias) > MAX_CALIBRATION_BIAS:
        reasons.append(ConfidenceReason.CALIBRATION_BIAS)
    if inputs.days_to_kickoff > PRELIMINARY_DAYS:
        reasons.append(ConfidenceReason.FAR_FROM_KICKOFF)
    return tuple(reasons)
