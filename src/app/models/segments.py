"""Segment statistics and the guarded decision to tighten or disable a segment (F8.5).

A *segment* is ``sport x league x market x odds bucket``. Over the settled signals of a
segment (retro excluded, hidden included: hiding is a view preference, not a selection
decision) this module computes the sample size, ROI, CLV and calibration bias, each with

- an **empirical-Bayes shrinkage** towards the global mean of all segments, and
- a **seeded block bootstrap** confidence interval (blocks = matchdays, so correlated
  results of one day are resampled together).

Estimator (documented in ADR 0013). For a metric with per-signal values ``v`` the segment's raw
mean is ``m`` over ``n`` signals; the global mean over every signal is ``g``. The shrunk
estimate is the normal-normal posterior mean with a prior worth ``PRIOR_STRENGTH`` (k)
pseudo-signals::

    shrunk = (n * m + k * g) / (n + k)

so a small segment is pulled to the global mean and a large one is left almost alone. ``k`` is a
fixed constant (not estimated from the data): with a handful of segments an estimated
between-segment variance is itself unstable, and a fixed, visible prior is easier to audit.
The interval is the bootstrap distribution of ``m`` pushed through the same shrinkage map
(``g`` held fixed), so the interval and the point estimate always agree.

The decision (:func:`segment_action`) is deliberately conservative. Nothing happens below
``MIN_SEGMENT_SAMPLE`` signals, a streak of losses never matters on its own (only an interval
that is entirely below zero does), restrictions are lifted only on positive point evidence
(a dead band between "significantly bad" and "clearly fine" keeps the state), and a restricted
state is held for a minimum time. Pure: no I/O, no database types.
"""

import math
import zlib
from collections.abc import Hashable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

import numpy as np
from numpy.typing import NDArray

MIN_SEGMENT_SAMPLE: Final = 50
"""No statistic is acted upon below this many settled signals (plan §6.13 C)."""
PRIOR_STRENGTH: Final = 30.0
"""Pseudo-signals of prior weight: a 50-signal segment keeps 62.5% of its own mean."""
BOOTSTRAP_RESAMPLES: Final = 1000
CI_LEVEL: Final = 0.95
DEFAULT_SEED: Final = 7
ODDS_BUCKET_EDGES: Final = (1.5, 1.8, 2.2, 3.0)
"""Upper edges of the odds buckets; the last bucket is open-ended."""
RAISED_EV_FLOOR: Final = 0.08
"""Minimum EV demanded in a RAISED segment (the default user bar is lower)."""
DISABLE_SHRUNK_ROI: Final = -0.05
RECOVERY_SHRUNK_ROI: Final = 0.0
MIN_HOLD_DAYS: Final = 14
DISABLE_COOLDOWN_DAYS: Final = 60


@dataclass(frozen=True, slots=True)
class SegmentKey:
    """Identity of a segment; ``league`` is any stable text id chosen by the caller."""

    sport: str
    league: str
    market: str
    odds_bucket: str


@dataclass(frozen=True, slots=True)
class SegmentObservation:
    """One settled signal as the statistics see it.

    ``unit_return`` is the profit per unit staked on the *recommended* odds (decision #17),
    ``hit`` whether the selection ended in profit, ``block`` the matchday (any integer that
    groups correlated results), ``clv`` the log closing-line value when known.
    """

    key: SegmentKey
    unit_return: float
    hit: bool
    p_model: float
    block: int
    clv: float | None = None


@dataclass(frozen=True, slots=True)
class Estimate:
    """A metric of a segment: raw mean, shrunk mean and a bootstrap interval of the shrunk mean."""

    n: int
    raw: float | None = None
    shrunk: float | None = None
    low: float | None = None
    high: float | None = None

    @property
    def available(self) -> bool:
        """True when at least one value contributed."""
        return self.raw is not None


@dataclass(frozen=True, slots=True)
class SegmentStats:
    """Everything known about one segment; ``n`` counts all settled signals."""

    key: SegmentKey
    n: int
    blocks: int
    roi: Estimate
    clv: Estimate
    calibration_bias: Estimate
    """Mean predicted probability minus observed hit rate (positive = overconfident)."""


@dataclass(frozen=True, slots=True)
class SegmentPolicy:
    """Named thresholds of the decision rules (tunable, but never inline)."""

    min_sample: int = MIN_SEGMENT_SAMPLE
    prior_strength: float = PRIOR_STRENGTH
    resamples: int = BOOTSTRAP_RESAMPLES
    ci_level: float = CI_LEVEL
    seed: int = DEFAULT_SEED
    raised_ev_floor: float = RAISED_EV_FLOOR
    disable_shrunk_roi: float = DISABLE_SHRUNK_ROI
    recovery_shrunk_roi: float = RECOVERY_SHRUNK_ROI
    min_hold_days: int = MIN_HOLD_DAYS
    disable_cooldown_days: int = DISABLE_COOLDOWN_DAYS


DEFAULT_SEGMENT_POLICY: Final = SegmentPolicy()


class SegmentState(StrEnum):
    """Restriction level of a segment."""

    ACTIVE = "active"
    RAISED = "raised"
    DISABLED = "disabled"


class SegmentAction(StrEnum):
    """What the weekly review does to a segment."""

    KEEP = "keep"
    RAISE_THRESHOLD = "raise_threshold"
    DISABLE = "disable"
    REENABLE = "reenable"


class SegmentReason(StrEnum):
    """Stable code of why a decision was taken (for the weekly learning report)."""

    TOO_FEW_SIGNALS = "too_few_signals"
    NO_SIGNIFICANT_EVIDENCE = "no_significant_evidence"
    SIGNIFICANT_NEGATIVE = "significant_negative"
    SEVERE_NEGATIVE = "severe_negative"
    RECOVERED = "recovered"
    HELD = "held"
    COOLDOWN = "cooldown"
    COOLDOWN_ELAPSED = "cooldown_elapsed"
    MANUAL_HOLD = "manual_hold"


@dataclass(frozen=True, slots=True)
class SegmentStatus:
    """Where a segment stands now: its state, since when, and an optional manual hold."""

    state: SegmentState = SegmentState.ACTIVE
    since: datetime | None = None
    hold_until: datetime | None = None
    """No automatic change before this moment (set when a person rolled a change back)."""


@dataclass(frozen=True, slots=True)
class SegmentDecision:
    """The outcome of :func:`segment_action`: action, resulting state and why."""

    action: SegmentAction
    state: SegmentState
    ev_floor: float
    reason: SegmentReason
    detail: str = ""


def odds_bucket(odds: float) -> str:
    """Label of the odds bucket of ``odds`` (e.g. ``"1.80-2.20"``, ``"3.00+"``)."""
    lower = 1.0
    for edge in ODDS_BUCKET_EDGES:
        if odds < edge:
            return f"{lower:.2f}-{edge:.2f}"
        lower = edge
    return f"{lower:.2f}+"


# --------------------------------------------------------------------------- statistics


def shrink(raw: float, n: int, global_mean: float, prior_strength: float) -> float:
    """Posterior mean of a segment mean pulled towards ``global_mean`` (see module docstring)."""
    weight = n / (n + prior_strength)
    return weight * raw + (1.0 - weight) * global_mean


def _block_totals(
    values: NDArray[np.float64], blocks: NDArray[np.int64]
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Per-block sum and count of ``values``."""
    _, inverse = np.unique(blocks, return_inverse=True)
    sums = np.bincount(inverse, weights=values)
    counts = np.bincount(inverse).astype(np.float64)
    return sums, counts


def _bootstrap_means(
    values: NDArray[np.float64],
    blocks: NDArray[np.int64],
    rng: np.random.Generator,
    resamples: int,
) -> NDArray[np.float64]:
    """Means of ``resamples`` block-bootstrap replicates (blocks drawn with replacement)."""
    sums, counts = _block_totals(values, blocks)
    draws = rng.integers(0, len(sums), size=(resamples, len(sums)))
    return np.asarray(sums[draws].sum(axis=1) / counts[draws].sum(axis=1), dtype=np.float64)


def _estimate(
    values: NDArray[np.float64],
    blocks: NDArray[np.int64],
    global_mean: float,
    policy: SegmentPolicy,
    seed_parts: Sequence[Hashable],
) -> Estimate:
    n = len(values)
    if n == 0:
        return Estimate(n=0)
    raw = float(values.mean())
    rng = np.random.default_rng([policy.seed, zlib.crc32(repr(tuple(seed_parts)).encode())])
    means = _bootstrap_means(values, blocks, rng, policy.resamples)
    tail = (1.0 - policy.ci_level) / 2.0
    low, high = (float(q) for q in np.quantile(means, [tail, 1.0 - tail]))
    return Estimate(
        n=n,
        raw=raw,
        shrunk=shrink(raw, n, global_mean, policy.prior_strength),
        low=shrink(low, n, global_mean, policy.prior_strength),
        high=shrink(high, n, global_mean, policy.prior_strength),
    )


def _mean_or_zero(values: Iterable[float]) -> float:
    items = list(values)
    return math.fsum(items) / len(items) if items else 0.0


def compute_segment_stats(
    observations: Sequence[SegmentObservation], policy: SegmentPolicy = DEFAULT_SEGMENT_POLICY
) -> dict[SegmentKey, SegmentStats]:
    """Statistics of every segment present in ``observations``.

    The global means (the shrinkage targets) are taken over all observations, so adding an
    unrelated segment moves the others only through that mean. Deterministic: every segment
    has its own seed derived from ``policy.seed`` and its key, so the result does not depend on
    the order of ``observations``.
    """
    observations = sorted(observations, key=_observation_order)
    global_roi = _mean_or_zero(o.unit_return for o in observations)
    global_clv = _mean_or_zero(o.clv for o in observations if o.clv is not None)
    global_bias = _mean_or_zero(o.p_model - float(o.hit) for o in observations)
    grouped: dict[SegmentKey, list[SegmentObservation]] = {}
    for observation in observations:
        grouped.setdefault(observation.key, []).append(observation)
    targets = (global_roi, global_clv, global_bias)
    return {
        key: _segment_stats(key, rows, targets, policy)
        for key, rows in sorted(grouped.items(), key=_key_order)
    }


def _observation_order(row: SegmentObservation) -> tuple[object, ...]:
    """A total order, so sums (and hence floats) never depend on the input order."""
    clv = row.clv
    return (
        row.key.sport,
        row.key.league,
        row.key.market,
        row.key.odds_bucket,
        row.block,
        row.unit_return,
        row.p_model,
        row.hit,
        clv is None,
        0.0 if clv is None else clv,
    )


def _key_order(item: tuple[SegmentKey, object]) -> tuple[str, str, str, str]:
    key = item[0]
    return (key.sport, key.league, key.market, key.odds_bucket)


def _segment_stats(
    key: SegmentKey,
    rows: Sequence[SegmentObservation],
    targets: tuple[float, float, float],
    policy: SegmentPolicy,
) -> SegmentStats:
    ordered = sorted(rows, key=_observation_order)
    blocks = np.array([row.block for row in ordered], dtype=np.int64)
    returns = np.array([row.unit_return for row in ordered], dtype=np.float64)
    bias = np.array([row.p_model - float(row.hit) for row in ordered], dtype=np.float64)
    with_clv = [row for row in ordered if row.clv is not None]
    clv_values = np.array([row.clv for row in with_clv], dtype=np.float64)
    clv_blocks = np.array([row.block for row in with_clv], dtype=np.int64)
    seed = (key.sport, key.league, key.market, key.odds_bucket)
    global_roi, global_clv, global_bias = targets
    return SegmentStats(
        key=key,
        n=len(ordered),
        blocks=len(set(blocks.tolist())),
        roi=_estimate(returns, blocks, global_roi, policy, (*seed, "roi")),
        clv=_estimate(clv_values, clv_blocks, global_clv, policy, (*seed, "clv")),
        calibration_bias=_estimate(bias, blocks, global_bias, policy, (*seed, "bias")),
    )


# --------------------------------------------------------------------------- decisions


def _clv_usable(stats: SegmentStats, policy: SegmentPolicy) -> bool:
    return stats.clv.n >= policy.min_sample and stats.clv.high is not None


def _below_zero(estimate: Estimate) -> bool:
    return estimate.high is not None and estimate.high < 0.0


def _significantly_negative(stats: SegmentStats, policy: SegmentPolicy) -> bool:
    """The whole interval of ROI (or of CLV, when it has enough signals) is below zero."""
    return _below_zero(stats.roi) or (_clv_usable(stats, policy) and _below_zero(stats.clv))


def _severely_negative(stats: SegmentStats, policy: SegmentPolicy) -> bool:
    """ROI is significantly and materially negative, and CLV (when usable) agrees."""
    shrunk = stats.roi.shrunk
    if shrunk is None or shrunk > policy.disable_shrunk_roi or not _below_zero(stats.roi):
        return False
    return not _clv_usable(stats, policy) or _below_zero(stats.clv)


def _recovered(stats: SegmentStats, policy: SegmentPolicy) -> bool:
    """Point evidence is clearly fine: positive shrunk ROI and non-negative CLV."""
    roi = stats.roi.shrunk
    if roi is None or roi <= policy.recovery_shrunk_roi:
        return False
    clv = stats.clv.shrunk
    return not _clv_usable(stats, policy) or (clv is not None and clv >= 0.0)


def _decision(
    action: SegmentAction,
    state: SegmentState,
    reason: SegmentReason,
    policy: SegmentPolicy,
    detail: str = "",
) -> SegmentDecision:
    floor = 0.0 if state is SegmentState.ACTIVE else policy.raised_ev_floor
    return SegmentDecision(action, state, floor, reason, detail)


def _held(status: SegmentStatus, now: datetime, policy: SegmentPolicy) -> bool:
    return status.since is not None and now - status.since < timedelta(days=policy.min_hold_days)


def _from_disabled(status: SegmentStatus, now: datetime, policy: SegmentPolicy) -> SegmentDecision:
    """A disabled segment produces no new signals, so it comes back on a timer, on probation."""
    cooldown = timedelta(days=policy.disable_cooldown_days)
    if status.since is None or now - status.since >= cooldown:
        return _decision(
            SegmentAction.REENABLE,
            SegmentState.RAISED,
            SegmentReason.COOLDOWN_ELAPSED,
            policy,
            "back on probation with a raised EV bar",
        )
    return _decision(SegmentAction.KEEP, SegmentState.DISABLED, SegmentReason.COOLDOWN, policy)


def _from_active(stats: SegmentStats, policy: SegmentPolicy) -> SegmentDecision:
    if _severely_negative(stats, policy):
        return _decision(
            SegmentAction.DISABLE, SegmentState.DISABLED, SegmentReason.SEVERE_NEGATIVE, policy
        )
    if _significantly_negative(stats, policy):
        return _decision(
            SegmentAction.RAISE_THRESHOLD,
            SegmentState.RAISED,
            SegmentReason.SIGNIFICANT_NEGATIVE,
            policy,
        )
    return _decision(
        SegmentAction.KEEP, SegmentState.ACTIVE, SegmentReason.NO_SIGNIFICANT_EVIDENCE, policy
    )


def _from_raised(
    stats: SegmentStats, status: SegmentStatus, now: datetime, policy: SegmentPolicy
) -> SegmentDecision:
    if _severely_negative(stats, policy):
        return _decision(
            SegmentAction.DISABLE, SegmentState.DISABLED, SegmentReason.SEVERE_NEGATIVE, policy
        )
    if _held(status, now, policy):
        return _decision(SegmentAction.KEEP, SegmentState.RAISED, SegmentReason.HELD, policy)
    if _recovered(stats, policy) and not _significantly_negative(stats, policy):
        return _decision(
            SegmentAction.REENABLE, SegmentState.ACTIVE, SegmentReason.RECOVERED, policy
        )
    return _decision(
        SegmentAction.KEEP, SegmentState.RAISED, SegmentReason.NO_SIGNIFICANT_EVIDENCE, policy
    )


def segment_action(
    stats: SegmentStats,
    policy: SegmentPolicy = DEFAULT_SEGMENT_POLICY,
    *,
    status: SegmentStatus = SegmentStatus(),  # noqa: B008 - frozen, shared default is safe
    now: datetime,
) -> SegmentDecision:
    """Decide what to do with a segment: keep, raise its EV bar, disable or lift a restriction.

    Rules, in order: a manual hold freezes everything; a disabled segment waits out its
    cooldown (no data can arrive for it) and then returns on probation; below
    ``policy.min_sample`` signals nothing changes; otherwise only an interval entirely below
    zero tightens a segment and only positive point evidence (outside the dead band) loosens
    it, after ``policy.min_hold_days`` in the restricted state.
    """
    if status.hold_until is not None and now < status.hold_until:
        return _decision(SegmentAction.KEEP, status.state, SegmentReason.MANUAL_HOLD, policy)
    if status.state is SegmentState.DISABLED:
        return _from_disabled(status, now, policy)
    if stats.n < policy.min_sample:
        return _decision(SegmentAction.KEEP, status.state, SegmentReason.TOO_FEW_SIGNALS, policy)
    if status.state is SegmentState.RAISED:
        return _from_raised(stats, status, now, policy)
    return _from_active(stats, policy)
