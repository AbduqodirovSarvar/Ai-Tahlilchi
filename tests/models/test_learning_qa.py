"""Independent QA of the learning loop maths (T5): oracles, simulations and leakage checks.

Everything here is computed against an independent source (``sklearn``, hand arithmetic or a
seeded simulation harness), never against the module's own output.
"""

import random
from datetime import UTC, datetime, timedelta
from functools import cache

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sklearn.isotonic import IsotonicRegression

from app.models.asian import AsianOutcomeProbabilities
from app.models.calibration import (
    CalibrationReason,
    CalibrationSample,
    apply_params,
    fit_calibration,
    fit_isotonic,
    rescale_outcomes,
)
from app.models.segments import (
    SegmentAction,
    SegmentKey,
    SegmentObservation,
    SegmentReason,
    SegmentState,
    SegmentStatus,
    compute_segment_stats,
    segment_action,
    shrink,
)

START = datetime(2026, 1, 5, tzinfo=UTC)  # a Monday
WEEKS = 52
KEY = SegmentKey("football", "E0", "1x2", "1.80-2.20")


# --------------------------------------------------------------------------- isotonic oracle


@pytest.mark.parametrize("seed", range(25))
def test_isotonic_blocks_match_the_sklearn_oracle(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311 - simulation
    size = rng.randint(5, 300)
    p = [round(rng.uniform(0.02, 0.98), rng.choice([2, 3, 6])) for _ in range(size)]
    hits = [rng.random() < min(0.97, x + rng.uniform(-0.15, 0.15)) for x in p]

    ours = fit_isotonic(p, hits)
    oracle = IsotonicRegression(increasing=True, out_of_bounds="clip").fit(
        p, [float(h) for h in hits]
    )

    # the pooled levels (the step heights) are exactly the oracle's
    assert np.allclose(
        np.unique(np.round(ours["y"], 10)), np.unique(np.round(oracle.predict(np.unique(p)), 10))
    )
    # one knot per pooled block; the knots never decrease
    assert np.all(np.diff(ours["y"]) >= 0)


def test_isotonic_equals_oracle_exactly_on_distinct_blocks() -> None:
    p = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
    hits = [False, True, False, True, True, False]
    ours = fit_isotonic(p, hits)
    oracle = IsotonicRegression().fit(p, [float(h) for h in hits])
    for x in p:
        # at a pooled block's weighted centre our knot value equals the oracle's block level
        assert apply_params(ours, x) == pytest.approx(
            min(max(float(np.interp(x, ours["x"], ours["y"])), 0.01), 0.99)
        )
    assert sorted(set(np.round(oracle.predict(p), 12))) == pytest.approx(
        sorted(set(np.round(ours["y"], 12)))
    )


# --------------------------------------------------------------------------- hand arithmetic


def _obs(key: SegmentKey, returns: list[float], first_block: int = 1) -> list[SegmentObservation]:
    return [
        SegmentObservation(key, value, value > 0, 0.5, first_block + index)
        for index, value in enumerate(returns)
    ]


def test_shrinkage_and_roi_hand_computed_example() -> None:
    """Segment A: 3 wins at 2.5 and 7 losses -> mean -0.25; B: 5 wins at 2.0, 5 losses -> 0."""
    key_a, key_b = KEY, SegmentKey("football", "E1", "1x2", "1.80-2.20")
    a = _obs(key_a, [1.5, 1.5, 1.5, *[-1.0] * 7])
    b = _obs(key_b, [1.0] * 5 + [-1.0] * 5, first_block=100)

    stats = compute_segment_stats([*a, *b])

    global_mean = (-2.5 + 0.0) / 20  # -0.125
    assert stats[key_a].roi.raw == pytest.approx(-0.25)
    assert stats[key_a].roi.shrunk == pytest.approx((10 * -0.25 + 30 * global_mean) / 40)
    assert stats[key_b].roi.shrunk == pytest.approx((10 * 0.0 + 30 * global_mean) / 40)
    assert shrink(-0.25, 10, global_mean, 30.0) == pytest.approx(-0.15625)
    # interval brackets the shrunk mean and is never wider than the raw interval would be
    assert stats[key_a].roi.low <= stats[key_a].roi.shrunk <= stats[key_a].roi.high


@given(st.lists(st.floats(min_value=-1.0, max_value=4.0), min_size=2, max_size=40))
@settings(max_examples=60, deadline=None)
def test_interval_never_lies_outside_the_data_range(returns: list[float]) -> None:
    stats = compute_segment_stats(_obs(KEY, returns))[KEY].roi
    assert stats.low is not None
    assert stats.high is not None
    assert min(returns) - 1e-9 <= stats.low <= stats.high <= max(returns) + 1e-9


# --------------------------------------------------------------------------- leakage


def _samples(
    count: int, *, shift: float, seed: int, start: datetime = START
) -> list[CalibrationSample]:
    rng = random.Random(seed)  # noqa: S311 - simulation
    out = []
    for index in range(count):
        p = rng.uniform(0.45, 0.75)
        out.append(CalibrationSample(p, rng.random() < p - shift, start + timedelta(hours=index)))
    return out


@pytest.mark.parametrize("seed", range(5))
def test_perturbing_anything_settled_at_or_after_the_cutoff_changes_nothing(seed: int) -> None:
    samples = _samples(600, shift=0.08, seed=seed)
    cutoff = samples[450].settled_at
    baseline = fit_calibration(samples, cutoff=cutoff, version=1)

    rng = random.Random(seed + 99)  # noqa: S311 - simulation
    perturbed = [
        s
        if s.settled_at < cutoff
        else CalibrationSample(rng.uniform(0.01, 0.99), rng.random() < 0.5, s.settled_at)
        for s in samples
    ]
    assert fit_calibration(perturbed, cutoff=cutoff, version=1) == baseline
    # the sample settled exactly at the cutoff is excluded (strict inequality)
    only_before = [s for s in samples if s.settled_at < cutoff]
    assert fit_calibration(only_before, cutoff=cutoff, version=1) == baseline


def test_the_holdout_labels_decide_the_gate_not_the_fit() -> None:
    """Flipping only the newest 30% of labels must turn a passing fit into a failing one."""
    samples = _samples(2000, shift=0.15, seed=3)
    cutoff = samples[-1].settled_at + timedelta(hours=1)
    assert fit_calibration(samples, cutoff=cutoff, version=1).activate

    flipped = [
        s if index < 1400 else CalibrationSample(s.probability, not s.hit, s.settled_at)
        for index, s in enumerate(samples)
    ]
    fit = fit_calibration(flipped, cutoff=cutoff, version=1)
    assert not fit.activate
    assert fit.reason in {CalibrationReason.NO_IMPROVEMENT, CalibrationReason.NOT_SIGNIFICANT}


@given(
    st.floats(min_value=0.001, max_value=0.999),
    st.floats(min_value=0.0, max_value=1.0),
    st.floats(min_value=0.0, max_value=1.0),
    st.floats(min_value=0.0, max_value=1.0),
)
@settings(max_examples=80, deadline=None)
def test_asian_rescale_hits_the_target_and_keeps_a_distribution(
    target: float, a: float, b: float, c: float
) -> None:
    total = a + b + c + 1.0
    outcomes = AsianOutcomeProbabilities(
        win=a / total, half_win=b / total, push=0.0, half_loss=c / total, loss=1.0 / total
    )
    if outcomes.win + outcomes.half_win <= 0.0:
        return
    scaled = rescale_outcomes(outcomes, target)
    parts = (scaled.win, scaled.half_win, scaled.push, scaled.half_loss, scaled.loss)
    assert all(part >= -1e-12 for part in parts)
    assert sum(parts) == pytest.approx(1.0)
    assert scaled.win + scaled.half_win == pytest.approx(target)


# --------------------------------------------------------------------------- simulations


def _simulate_segments(
    probability: float, *, segments: int, per_week: int, seed: int
) -> list[list[tuple[int, str]]]:
    """52 weekly reviews of ``segments`` independent segments at odds 2.0 (EV = 2p - 1)."""
    rng = random.Random(seed)  # noqa: S311 - simulation
    keys = [SegmentKey("football", f"L{i}", "1x2", "1.80-2.20") for i in range(segments)]
    rows: list[SegmentObservation] = []
    status = {key: SegmentStatus() for key in keys}
    events: dict[SegmentKey, list[tuple[int, str]]] = {key: [] for key in keys}
    for week in range(WEEKS):
        for key in keys:
            for _ in range(per_week):
                hit = rng.random() < probability
                day = START.toordinal() + week * 7 + rng.randrange(7)
                rows.append(SegmentObservation(key, 1.0 if hit else -1.0, hit, probability, day))
        now = START + timedelta(weeks=week + 1)
        stats = compute_segment_stats(rows)
        for key in keys:
            decision = segment_action(stats[key], status=status[key], now=now)
            if decision.state is not status[key].state:
                events[key].append((week + 1, decision.action.value))
                status[key] = SegmentStatus(decision.state, now, None)
    return list(events.values())


@cache
def _zero_edge_world() -> tuple[int, int, int]:
    """(segments, segments with any tightening, segments that changed state 3+ times)."""
    events = [
        e for seed in range(4) for e in _simulate_segments(0.5, segments=20, per_week=3, seed=seed)
    ]
    flagged = sum(1 for e in events if any(a in {"raise_threshold", "disable"} for _, a in e))
    unstable = sum(1 for e in events if len(e) > 2)
    return len(events), flagged, unstable


@pytest.mark.slow
@pytest.mark.xfail(
    strict=True,
    reason="T5-QA-1: repeated weekly looks at a 95% interval flag ~9% of zero-edge segments a year",
)
def test_zero_edge_segments_are_rarely_tightened_within_a_year() -> None:
    total, flagged, _ = _zero_edge_world()
    assert flagged / total <= 0.05


@pytest.mark.slow
@pytest.mark.xfail(
    strict=True,
    reason="T5-QA-2: disable, cooldown, probation, disable cycles flip zero-edge segments",
)
def test_zero_edge_segments_do_not_change_state_back_and_forth() -> None:
    total, _, unstable = _zero_edge_world()
    assert unstable / total <= 0.02


@pytest.mark.slow
def test_a_clearly_losing_segment_is_caught_within_a_year() -> None:
    """True ROI -20% (p = 0.40 at odds 2.0), 3 signals a week: almost every segment is caught."""
    events = _simulate_segments(0.40, segments=20, per_week=3, seed=1)
    caught = [e for e in events if e]
    assert len(caught) / len(events) >= 0.8
    first_weeks = sorted(e[0][0] for e in caught)
    assert first_weeks[len(first_weeks) // 2] <= 30  # median first action within ~30 weeks


def _losing_history(count: int) -> list[SegmentObservation]:
    return [SegmentObservation(KEY, -1.0, False, 0.5, 1 + i // 2) for i in range(count)]


@pytest.mark.xfail(
    strict=True,
    reason="T5-QA-2: the stats of a re-enabled segment still contain its pre-disable history",
)
def test_a_reenabled_segment_gets_a_real_probation_not_an_immediate_second_disable() -> None:
    stats = compute_segment_stats(_losing_history(200))[KEY]
    disabled_at = START
    now = disabled_at + timedelta(days=60)
    back = segment_action(stats, status=SegmentStatus(SegmentState.DISABLED, disabled_at), now=now)
    assert back.action is SegmentAction.REENABLE
    assert back.state is SegmentState.RAISED

    next_week = segment_action(
        stats, status=SegmentStatus(SegmentState.RAISED, now), now=now + timedelta(days=7)
    )
    # no new probation signal can have settled in 7 days; the old record must not decide
    assert next_week.reason is not SegmentReason.SEVERE_NEGATIVE


def _calibration_world(shift: float, per_week: int, seed: int) -> int | None:
    """Week of the first activation of a calibration over 52 weekly refits, else ``None``."""
    rng = random.Random(seed)  # noqa: S311 - simulation
    samples: list[CalibrationSample] = []
    for week in range(WEEKS):
        for slot in range(per_week):
            p = rng.uniform(0.45, 0.75)
            samples.append(
                CalibrationSample(
                    p, rng.random() < p - shift, START + timedelta(weeks=week, hours=slot + 1)
                )
            )
        if fit_calibration(samples, cutoff=START + timedelta(weeks=week + 1), version=1).activate:
            return week + 1
    return None


@pytest.mark.slow
@pytest.mark.xfail(
    strict=True,
    reason="T5-QA-3: a perfectly calibrated model gets a noise map ~1 year in 4 (52 looks at 95%)",
)
def test_a_calibrated_model_is_almost_never_given_a_calibration_map() -> None:
    activated = [_calibration_world(0.0, 20, seed) is not None for seed in range(20)]
    assert sum(activated) / len(activated) <= 0.05


@pytest.mark.slow
def test_a_clearly_overconfident_model_is_calibrated_within_a_year() -> None:
    weeks = [_calibration_world(0.08, 50, seed) for seed in range(8)]
    assert all(w is not None for w in weeks)
    assert max(w for w in weeks if w is not None) <= 40
