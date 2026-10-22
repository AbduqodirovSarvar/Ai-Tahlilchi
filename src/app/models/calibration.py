"""Isotonic probability calibration, fitted walk-forward and activated only on proof (F8.6).

If the model says "60%" and 52% of such picks win, later probabilities should be corrected.
This module fits a monotone map ``p -> p'`` (isotonic regression, pool-adjacent-violators) on
settled signals and decides whether it is *allowed to be used*:

1. **Leakage-safe.** Only samples settled strictly before the ``cutoff`` are used; anything
   later is ignored, so a fit made at time T never depends on what happened after T.
2. **Time split.** The eligible samples are ordered by settlement time; the earlier 70% train
   the map, the later 30% (the hold-out, which the fit never sees) score it.
3. **Gate.** The per-sample Brier gain ``(p-y)^2 - (f(p)-y)^2`` on the hold-out must average at
   least ``min_gain`` *and* its one-sided bootstrap lower bound must be above zero. A model
   that is already calibrated therefore never gets a map (the gain is noise), and a small
   sample never does either (the lower bound is below zero).
4. **Shipped map.** After the gate passes the map is refitted on all eligible samples (more
   data than the 70% that was tested); ``brier_before``/``brier_after`` are the hold-out
   numbers of the tested fit, which is the honest evidence, not an in-sample score.

Versions are plain data (JSON-serialisable ``params``), so the caller can store them, switch the
active one and roll back with :func:`select_rollback`. Pure: no I/O.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

import numpy as np
from numpy.typing import NDArray

from app.models._validation import check_probability
from app.models.asian import AsianOutcomeProbabilities
from app.models.exceptions import ModelInputError

METHOD_ISOTONIC: Final = "isotonic"
MIN_CALIBRATION_SAMPLES: Final = 200
MIN_HOLDOUT_SAMPLES: Final = 60
HOLDOUT_FRACTION: Final = 0.3
MIN_BRIER_GAIN: Final = 0.001
BOOTSTRAP_RESAMPLES: Final = 1000
ONE_SIDED_CONFIDENCE: Final = 0.95
DEFAULT_SEED: Final = 11
OUTPUT_FLOOR: Final = 0.01
OUTPUT_CEILING: Final = 0.99
"""Calibrated probabilities stay inside this band: a fitted step of 0 or 1 is never certainty."""
COMPLEMENT_SUM_TOLERANCE: Final = 1e-6


@dataclass(frozen=True, slots=True)
class CalibrationSample:
    """A settled signal: the probability that was predicted, whether it hit, when it settled."""

    probability: float
    hit: bool
    settled_at: datetime


@dataclass(frozen=True, slots=True)
class CalibrationPolicy:
    """Named thresholds of the fit and the activation gate."""

    min_samples: int = MIN_CALIBRATION_SAMPLES
    min_holdout: int = MIN_HOLDOUT_SAMPLES
    holdout_fraction: float = HOLDOUT_FRACTION
    min_gain: float = MIN_BRIER_GAIN
    resamples: int = BOOTSTRAP_RESAMPLES
    confidence: float = ONE_SIDED_CONFIDENCE
    seed: int = DEFAULT_SEED


DEFAULT_CALIBRATION_POLICY: Final = CalibrationPolicy()


@dataclass(frozen=True, slots=True)
class CalibrationVersion:
    """A fitted calibration. ``passed_gate`` says whether it may be activated."""

    version: int
    method: str
    params: dict[str, list[float]]
    n_samples: int
    brier_before: float
    brier_after: float
    created_at: datetime
    passed_gate: bool


class CalibrationReason(StrEnum):
    """Why a fit was or was not eligible for activation."""

    TOO_FEW_SAMPLES = "too_few_samples"
    NO_IMPROVEMENT = "no_improvement"
    NOT_SIGNIFICANT = "not_significant"
    IMPROVED = "improved"


@dataclass(frozen=True, slots=True)
class CalibrationFit:
    """Result of :func:`fit_calibration`; ``version`` is ``None`` when there was too little data."""

    reason: CalibrationReason
    version: CalibrationVersion | None = None
    gain: float | None = None
    gain_low: float | None = None
    n_train: int = 0
    n_holdout: int = 0

    @property
    def activate(self) -> bool:
        """True when the fit passed the gate."""
        return self.version is not None and self.version.passed_gate


# --------------------------------------------------------------------------- isotonic fit


def _pool_adjacent_violators(
    x: NDArray[np.float64], y: NDArray[np.float64], weight: NDArray[np.float64]
) -> tuple[list[float], list[float]]:
    """Non-decreasing least-squares fit of ``y`` on increasing ``x``; one knot per pooled block."""
    blocks: list[list[float]] = []  # [total weight, weighted y, weighted x]
    for xi, yi, wi in zip(x.tolist(), y.tolist(), weight.tolist(), strict=True):
        blocks.append([wi, wi * yi, wi * xi])
        while len(blocks) > 1 and blocks[-2][1] / blocks[-2][0] > blocks[-1][1] / blocks[-1][0]:
            top = blocks.pop()
            blocks[-1] = [a + b for a, b in zip(blocks[-1], top, strict=True)]
    return [b[2] / b[0] for b in blocks], [b[1] / b[0] for b in blocks]


def fit_isotonic(probabilities: Sequence[float], hits: Sequence[bool]) -> dict[str, list[float]]:
    """Knots ``{"x": [...], "y": [...]}`` of the monotone map fitted on the samples.

    Equal probabilities are pooled first, so ``x`` is strictly increasing; ``y`` is
    non-decreasing by construction.

    Raises:
        ModelInputError: no samples, mismatched lengths or an invalid probability.
    """
    if not probabilities or len(probabilities) != len(hits):
        msg = "isotonic fit needs equally many (>= 1) probabilities and outcomes"
        raise ModelInputError(msg)
    p = np.array([check_probability(value) for value in probabilities], dtype=np.float64)
    h = np.array(hits, dtype=np.float64)
    unique, inverse = np.unique(p, return_inverse=True)
    counts = np.bincount(inverse).astype(np.float64)
    mean_hit = np.bincount(inverse, weights=h) / counts
    xs, ys = _pool_adjacent_violators(unique, mean_hit, counts)
    return {"x": xs, "y": ys}


def apply_params(params: dict[str, list[float]], probability: float) -> float:
    """Calibrated ``probability`` under knots ``params`` (linear between knots, flat outside)."""
    value = float(np.interp(check_probability(probability), params["x"], params["y"]))
    return min(max(value, OUTPUT_FLOOR), OUTPUT_CEILING)


def apply_calibration(version: CalibrationVersion, probability: float) -> float:
    """Calibrated ``probability`` under ``version``, always inside the output band."""
    if version.method != METHOD_ISOTONIC:
        msg = f"unknown calibration method {version.method!r}"
        raise ModelInputError(msg)
    return apply_params(version.params, probability)


def calibrate_complementary(
    version: CalibrationVersion, probabilities: Sequence[float]
) -> list[float]:
    """Calibrate the outcomes of one market that partition the result and renormalise them.

    The input must sum to one (within tolerance); the output does too, so a calibrated 1X2 or
    over/under pair is still a distribution.

    Raises:
        ModelInputError: the probabilities do not sum to one.
    """
    total = math.fsum(probabilities)
    if abs(total - 1.0) > COMPLEMENT_SUM_TOLERANCE:
        msg = f"complementary probabilities must sum to 1, got {total!r}"
        raise ModelInputError(msg)
    calibrated = [apply_calibration(version, p) for p in probabilities]
    scale = math.fsum(calibrated)
    return [value / scale for value in calibrated]


def rescale_outcomes(
    outcomes: AsianOutcomeProbabilities, win_probability: float
) -> AsianOutcomeProbabilities:
    """The settlement distribution with ``win + half_win`` replaced by ``win_probability``.

    The winning side and the push/losing side are each scaled proportionally, so the shape of an
    Asian bet (how much of the win is a half win) is kept while EV follows the calibrated number.
    """
    current = outcomes.win + outcomes.half_win
    target = check_probability(win_probability)
    if current <= 0.0:
        return outcomes
    up = target / current
    rest = 1.0 - current
    if rest <= 0.0:
        return AsianOutcomeProbabilities(
            target * outcomes.win / current,
            target * outcomes.half_win / current,
            0.0,
            0.0,
            1.0 - target,
        )
    down = (1.0 - target) / rest
    return AsianOutcomeProbabilities(
        outcomes.win * up,
        outcomes.half_win * up,
        outcomes.push * down,
        outcomes.half_loss * down,
        outcomes.loss * down,
    )


# --------------------------------------------------------------------------- walk-forward


def _squared_errors(
    params: dict[str, list[float]] | None, p: NDArray[np.float64], h: NDArray[np.float64]
) -> NDArray[np.float64]:
    predicted = (
        p
        if params is None
        else np.clip(np.interp(p, params["x"], params["y"]), OUTPUT_FLOOR, OUTPUT_CEILING)
    )
    return np.asarray((predicted - h) ** 2, dtype=np.float64)


def _gain_lower_bound(gains: NDArray[np.float64], policy: CalibrationPolicy) -> float:
    """One-sided bootstrap lower bound of the mean per-sample Brier gain."""
    rng = np.random.default_rng(policy.seed)
    draws = rng.integers(0, len(gains), size=(policy.resamples, len(gains)))
    means = gains[draws].mean(axis=1)
    return float(np.quantile(means, 1.0 - policy.confidence))


def _eligible(samples: Sequence[CalibrationSample], cutoff: datetime) -> list[CalibrationSample]:
    """Samples settled strictly before ``cutoff``, in a total, input-order-independent order."""
    kept = [s for s in samples if s.settled_at < cutoff]
    return sorted(kept, key=lambda s: (s.settled_at, s.probability, s.hit))


def fit_calibration(
    samples: Sequence[CalibrationSample],
    *,
    cutoff: datetime,
    version: int,
    policy: CalibrationPolicy = DEFAULT_CALIBRATION_POLICY,
) -> CalibrationFit:
    """Fit a calibration on what was settled before ``cutoff`` and judge it on a later hold-out.

    ``version`` and ``cutoff`` (also the creation time) are supplied by the caller so the call
    is a pure function of its inputs: the same samples give the same fit.
    """
    ordered = _eligible(samples, cutoff)
    n_holdout = max(round(len(ordered) * policy.holdout_fraction), policy.min_holdout)
    if len(ordered) < policy.min_samples or n_holdout >= len(ordered):
        return CalibrationFit(CalibrationReason.TOO_FEW_SAMPLES)
    train, holdout = ordered[:-n_holdout], ordered[-n_holdout:]
    tested = fit_isotonic([s.probability for s in train], [s.hit for s in train])
    p = np.array([s.probability for s in holdout], dtype=np.float64)
    h = np.array([s.hit for s in holdout], dtype=np.float64)
    before = _squared_errors(None, p, h)
    after = _squared_errors(tested, p, h)
    gains = before - after
    gain, low = float(gains.mean()), _gain_lower_bound(gains, policy)
    reason = _reason(gain, low, policy)
    shipped = fit_isotonic([s.probability for s in ordered], [s.hit for s in ordered])
    return CalibrationFit(
        reason=reason,
        version=CalibrationVersion(
            version=version,
            method=METHOD_ISOTONIC,
            params=shipped,
            n_samples=len(ordered),
            brier_before=float(before.mean()),
            brier_after=float(after.mean()),
            created_at=cutoff,
            passed_gate=reason is CalibrationReason.IMPROVED,
        ),
        gain=gain,
        gain_low=low,
        n_train=len(train),
        n_holdout=len(holdout),
    )


def _reason(gain: float, low: float, policy: CalibrationPolicy) -> CalibrationReason:
    if gain < policy.min_gain:
        return CalibrationReason.NO_IMPROVEMENT
    if low <= 0.0:
        return CalibrationReason.NOT_SIGNIFICANT
    return CalibrationReason.IMPROVED


def select_rollback(
    versions: Sequence[CalibrationVersion], active: CalibrationVersion
) -> CalibrationVersion | None:
    """The version to fall back to from ``active``: the newest earlier one that passed the gate.

    ``None`` means "no calibration" (the raw model), because nothing earlier was ever eligible.
    """
    earlier = [v for v in versions if v.version < active.version and v.passed_gate]
    return max(earlier, key=lambda v: v.version, default=None)
