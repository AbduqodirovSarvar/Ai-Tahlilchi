"""Segment statistics, shrinkage and the guarded decision rules (F8.5)."""

import random
from datetime import UTC, datetime, timedelta
from functools import cache
from itertools import pairwise

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.models.segments import (
    DEFAULT_SEGMENT_POLICY,
    Estimate,
    SegmentAction,
    SegmentKey,
    SegmentObservation,
    SegmentPolicy,
    SegmentReason,
    SegmentState,
    SegmentStats,
    SegmentStatus,
    compute_segment_stats,
    fdr_significant,
    odds_bucket,
    review_segments,
    segment_action,
    shrink,
)

NOW = datetime(2026, 11, 1, tzinfo=UTC)
KEY = SegmentKey("football", "api-football:39", "totals", "1.80-2.20")
OTHER = SegmentKey("football", "api-football:140", "1x2", "1.80-2.20")
FAST = SegmentPolicy(resamples=300)
FRESH = SegmentStatus()
CONFIRMED = SegmentStatus(flagged=True)
"""Last week's run already saw tightening evidence: this week may act on it."""


def simulate(  # noqa: PLR0913 - a simulation knob per argument
    key: SegmentKey,
    n: int,
    *,
    hit_rate: float,
    odds: float = 2.0,
    clv_mean: float | None = 0.0,
    seed: int = 1,
) -> list[SegmentObservation]:
    """``n`` flat-stake signals, one per matchday, hitting with ``hit_rate`` at ``odds``."""
    rng = random.Random(seed)  # noqa: S311 - simulation, not security
    rows = []
    for index in range(n):
        hit = rng.random() < hit_rate
        clv = None if clv_mean is None else rng.gauss(clv_mean, 0.05)
        rows.append(
            SegmentObservation(
                key=key,
                unit_return=(odds - 1.0) if hit else -1.0,
                hit=hit,
                p_model=0.5,
                block=index,
                clv=clv,
            )
        )
    return rows


def stats_of(rows: list[SegmentObservation], key: SegmentKey = KEY) -> SegmentStats:
    return compute_segment_stats(rows, FAST)[key]


def decide(rows: list[SegmentObservation], status: SegmentStatus = FRESH):
    return segment_action(stats_of(rows), FAST, status=status, now=NOW)


@pytest.mark.parametrize(
    ("odds", "label"),
    [
        (1.01, "1.00-1.50"),
        (1.5, "1.50-1.80"),
        (1.79, "1.50-1.80"),
        (2.0, "1.80-2.20"),
        (2.2, "2.20-3.00"),
        (3.0, "3.00+"),
        (15.0, "3.00+"),
    ],
)
def test_odds_bucket_edges(odds: float, label: str) -> None:
    assert odds_bucket(odds) == label


def test_shrinkage_hand_computed() -> None:
    # n=50, raw=-0.10, global=0.02, k=30: weight 50/80 = 0.625 -> 0.625 * -0.10 + 0.375 * 0.02
    assert shrink(-0.10, 50, 0.02, 30.0) == pytest.approx(-0.055)
    assert shrink(-0.10, 10**9, 0.02, 30.0) == pytest.approx(-0.10, abs=1e-6)
    assert shrink(-0.10, 0, 0.02, 30.0) == pytest.approx(0.02)


def test_stats_shrink_towards_global_mean() -> None:
    bad = simulate(KEY, 50, hit_rate=0.3, seed=2)
    good = simulate(OTHER, 400, hit_rate=0.6, seed=3)
    segment = compute_segment_stats([*bad, *good], FAST)[KEY]
    global_roi = sum(o.unit_return for o in [*bad, *good]) / 450
    assert segment.n == 50
    assert segment.roi.raw is not None
    assert segment.roi.shrunk == pytest.approx((50 * segment.roi.raw + 30 * global_roi) / 80)
    assert segment.roi.low is not None
    assert segment.roi.high is not None
    assert segment.roi.low <= segment.roi.shrunk <= segment.roi.high


def test_calibration_bias_is_mean_prediction_minus_hit_rate() -> None:
    rows = [SegmentObservation(KEY, 1.0, True, 0.6, block=i) for i in range(3)]
    rows += [SegmentObservation(KEY, -1.0, False, 0.6, block=i + 3) for i in range(2)]
    assert stats_of(rows).calibration_bias.raw == pytest.approx(0.6 - 3 / 5)
    rows.append(SegmentObservation(KEY, -1.0, False, 0.6, block=9))
    assert stats_of(rows).calibration_bias.raw == pytest.approx(0.6 - 3 / 6)


def test_clv_missing_is_unavailable() -> None:
    stats = stats_of(simulate(KEY, 60, hit_rate=0.5, clv_mean=None))
    assert not stats.clv.available
    assert stats.clv.n == 0


def test_stats_are_deterministic_and_order_independent() -> None:
    rows = simulate(KEY, 80, hit_rate=0.45, seed=5) + simulate(OTHER, 70, hit_rate=0.5, seed=6)
    first = compute_segment_stats(rows, FAST)
    shuffled = list(rows)
    random.Random(0).shuffle(shuffled)  # noqa: S311 - simulation
    assert compute_segment_stats(shuffled, FAST) == first


def test_empty_input() -> None:
    assert compute_segment_stats([], FAST) == {}


def test_block_bootstrap_is_wider_than_naive_for_correlated_days() -> None:
    rows = []
    for day in range(10):
        hit = day % 2 == 0
        rows += [
            SegmentObservation(KEY, 1.0 if hit else -1.0, hit, 0.5, block=day) for _ in range(10)
        ]
    spread = [
        SegmentObservation(KEY, o.unit_return, o.hit, o.p_model, block=index)
        for index, o in enumerate(rows)
    ]
    clustered, independent = stats_of(rows).roi, stats_of(spread).roi
    assert clustered.high is not None
    assert clustered.low is not None
    assert independent.high is not None
    assert independent.low is not None
    assert clustered.high - clustered.low > independent.high - independent.low


# ----------------------------------------------------------------- decisions


def test_a_real_negative_segment_is_disabled_only_with_enough_signals() -> None:
    rows = simulate(KEY, 120, hit_rate=0.30, clv_mean=-0.08)
    early = decide(rows[:49], CONFIRMED)
    assert early.action is SegmentAction.KEEP
    assert early.reason is SegmentReason.TOO_FEW_SIGNALS
    assert decide(rows[:50], CONFIRMED).action in {
        SegmentAction.RAISE_THRESHOLD,
        SegmentAction.DISABLE,
    }
    full = decide(rows, CONFIRMED)
    assert full.action is SegmentAction.DISABLE
    assert full.state is SegmentState.DISABLED
    assert full.reason is SegmentReason.SEVERE_NEGATIVE


def test_evidence_must_repeat_on_a_second_run_before_anything_changes() -> None:
    rows = simulate(KEY, 120, hit_rate=0.30, clv_mean=-0.08)
    first = decide(rows)  # nothing was flagged before
    assert first.action is SegmentAction.KEEP
    assert first.state is SegmentState.ACTIVE
    assert first.reason is SegmentReason.AWAITING_CONFIRMATION
    assert first.flagged
    assert decide(rows, SegmentStatus(flagged=first.flagged)).action is SegmentAction.DISABLE


def test_evidence_that_disappears_clears_the_flag() -> None:
    rows = simulate(KEY, 120, hit_rate=0.55, clv_mean=0.02)
    decision = decide(rows, CONFIRMED)
    assert decision.action is SegmentAction.KEEP
    assert not decision.flagged


@cache
def mildly_negative() -> list[SegmentObservation]:
    """ROI about -4% at odds 1.5 (hit rate 64%): too small to disable, real enough to raise."""
    return simulate(KEY, 4000, hit_rate=0.64, odds=1.5, clv_mean=-0.04, seed=8)


def test_mildly_negative_roi_with_negative_clv_raises_the_bar_not_disables() -> None:
    decision = decide(mildly_negative(), CONFIRMED)
    assert decision.action is SegmentAction.RAISE_THRESHOLD
    assert decision.state is SegmentState.RAISED
    assert decision.ev_floor == DEFAULT_SEGMENT_POLICY.raised_ev_floor


def test_negative_clv_alone_never_tightens_a_segment() -> None:
    rows = simulate(KEY, 150, hit_rate=0.52, odds=2.0, clv_mean=-0.04, seed=8)
    decision = decide(rows, CONFIRMED)
    assert decision.action is SegmentAction.KEEP
    assert decision.reason is SegmentReason.NO_SIGNIFICANT_EVIDENCE


def test_fdr_veto_blocks_a_segment_whose_interval_alone_would_act() -> None:
    rows = simulate(KEY, 120, hit_rate=0.30, clv_mean=-0.08)
    stats = stats_of(rows)
    blocked = segment_action(stats, FAST, status=CONFIRMED, now=NOW, fdr_ok=False)
    assert blocked.action is SegmentAction.KEEP
    assert blocked.reason is SegmentReason.NO_SIGNIFICANT_EVIDENCE


def test_a_bad_streak_in_a_good_segment_triggers_nothing() -> None:
    good = simulate(KEY, 100, hit_rate=0.62, clv_mean=0.04, seed=9)
    streak = [SegmentObservation(KEY, -1.0, False, 0.5, block=1000 + i, clv=0.04) for i in range(8)]
    decision = decide([*good, *streak])
    assert decision.action is SegmentAction.KEEP
    assert decision.state is SegmentState.ACTIVE
    assert decision.ev_floor == 0.0


def test_sample_below_minimum_never_acts_even_if_terrible() -> None:
    rows = [SegmentObservation(KEY, -1.0, False, 0.6, block=i, clv=-0.2) for i in range(49)]
    assert decide(rows).action is SegmentAction.KEEP


def test_hysteresis_dead_band_keeps_raised_state() -> None:
    rows = simulate(KEY, 80, hit_rate=0.5, odds=2.0, clv_mean=-0.005, seed=10)
    decision = decide(rows, SegmentStatus(SegmentState.RAISED, NOW - timedelta(days=30)))
    assert decision.state is SegmentState.RAISED
    assert decision.action is SegmentAction.KEEP


def test_raised_is_held_for_minimum_days_then_recovers() -> None:
    rows = simulate(KEY, 200, hit_rate=0.65, clv_mean=0.05, seed=11)
    recent = SegmentStatus(SegmentState.RAISED, NOW - timedelta(days=3))
    assert decide(rows, recent).reason is SegmentReason.HELD
    old = SegmentStatus(SegmentState.RAISED, NOW - timedelta(days=20))
    decision = decide(rows, old)
    assert decision.action is SegmentAction.REENABLE
    assert decision.state is SegmentState.ACTIVE


def test_raised_is_judged_after_the_hold_not_during_it() -> None:
    rows = simulate(KEY, 120, hit_rate=0.28, clv_mean=-0.09, seed=12)
    during = SegmentStatus(SegmentState.RAISED, NOW - timedelta(days=1), flagged=True)
    assert decide(rows, during).reason is SegmentReason.HELD
    after = SegmentStatus(SegmentState.RAISED, NOW - timedelta(days=15), flagged=True)
    assert decide(rows, after).action is SegmentAction.DISABLE
    unconfirmed = SegmentStatus(SegmentState.RAISED, NOW - timedelta(days=15))
    assert decide(rows, unconfirmed).reason is SegmentReason.AWAITING_CONFIRMATION


def test_disabled_waits_for_cooldown_then_returns_on_probation() -> None:
    rows = simulate(KEY, 100, hit_rate=0.3, clv_mean=-0.08)
    waiting = decide(rows, SegmentStatus(SegmentState.DISABLED, NOW - timedelta(days=10)))
    assert waiting.action is SegmentAction.KEEP
    assert waiting.state is SegmentState.DISABLED
    assert waiting.reason is SegmentReason.COOLDOWN
    back = decide(rows, SegmentStatus(SegmentState.DISABLED, NOW - timedelta(days=61)))
    assert back.action is SegmentAction.REENABLE
    assert back.state is SegmentState.RAISED
    assert back.ev_floor > 0.0


def test_disabled_without_a_start_time_comes_back() -> None:
    back = decide(simulate(KEY, 3, hit_rate=0.5), SegmentStatus(SegmentState.DISABLED, None))
    assert back.action is SegmentAction.REENABLE


def test_manual_hold_freezes_everything() -> None:
    rows = simulate(KEY, 120, hit_rate=0.28, clv_mean=-0.09, seed=13)
    status = SegmentStatus(SegmentState.ACTIVE, None, hold_until=NOW + timedelta(days=7))
    decision = decide(rows, status)
    assert decision.action is SegmentAction.KEEP
    assert decision.reason is SegmentReason.MANUAL_HOLD


def test_few_signals_keeps_current_state_and_floor() -> None:
    status = SegmentStatus(SegmentState.RAISED, NOW - timedelta(days=40))
    decision = decide(simulate(KEY, 10, hit_rate=0.5), status)
    assert decision.state is SegmentState.RAISED
    assert decision.ev_floor == DEFAULT_SEGMENT_POLICY.raised_ev_floor


def test_no_flip_flop_over_weeks() -> None:
    """Re-running the review on unchanged data every week never oscillates."""
    rows = mildly_negative()
    status = SegmentStatus()
    history = []
    for week in range(6):
        now = NOW + timedelta(weeks=week)
        decision = segment_action(stats_of(rows), FAST, status=status, now=now)
        since = now if decision.state is not status.state else status.since
        status = SegmentStatus(decision.state, since, flagged=decision.flagged)
        history.append(status.state)
    changes = sum(1 for a, b in pairwise(history) if a is not b)
    assert history[0] is SegmentState.ACTIVE  # the first run only flags
    assert history[1] is SegmentState.RAISED  # the second confirms
    assert changes == 1


def test_a_probation_segment_is_judged_only_on_data_since_the_probation_began() -> None:
    """The pre-probation losses must not decide the verdict of the trial (T5-QA-2)."""
    history = [SegmentObservation(KEY, -1.0, False, 0.5, block=i, clv=-0.2) for i in range(200)]
    began = NOW - timedelta(days=15)
    fresh_block = began.date().toordinal() + 1
    trial = [SegmentObservation(KEY, 0.0, True, 0.5, block=fresh_block + i) for i in range(5)]
    status = SegmentStatus(SegmentState.RAISED, began, flagged=True)

    review = review_segments([*history, *trial], {KEY: status}, now=NOW, policy=FAST)

    assert review.stats[KEY].n == 5  # only the trial counts
    assert review.decisions[KEY].reason is SegmentReason.TOO_FEW_SIGNALS
    assert review.decisions[KEY].state is SegmentState.RAISED


def test_review_keeps_a_fully_filtered_probation_segment_in_the_result() -> None:
    history = [SegmentObservation(KEY, -1.0, False, 0.5, block=i) for i in range(60)]
    status = SegmentStatus(SegmentState.RAISED, NOW - timedelta(days=15))
    review = review_segments(history, {KEY: status}, now=NOW, policy=FAST)
    assert review.stats[KEY].n == 0
    assert review.decisions[KEY].state is SegmentState.RAISED


def with_p_value(index: int, p_value: float) -> tuple[SegmentKey, SegmentStats]:
    key = SegmentKey("football", f"L{index}", "1x2", "1.80-2.20")
    roi = Estimate(n=100, raw=-0.1, shrunk=-0.1, low=-0.2, high=-0.01, p_value=p_value)
    return key, SegmentStats(key, 100, 100, roi, Estimate(n=0), Estimate(n=0))


def test_fdr_guard_matches_the_benjamini_hochberg_textbook_example() -> None:
    p_values = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.216]
    family = dict(with_p_value(i, p) for i, p in enumerate(p_values))
    flagged = fdr_significant(family, SegmentPolicy(fdr_level=0.05))
    assert flagged == {SegmentKey("football", f"L{i}", "1x2", "1.80-2.20") for i in (0, 1)}


def test_fdr_guard_is_stricter_with_many_segments() -> None:
    """The same borderline p-value passes alone and fails among many segments."""
    alone = dict([with_p_value(0, 0.04)])
    crowd = dict([with_p_value(0, 0.04), *(with_p_value(i, 0.5) for i in range(1, 20))])
    assert len(fdr_significant(alone)) == 1
    assert fdr_significant(crowd) == frozenset()


def test_fdr_on_nothing_flags_nothing() -> None:
    assert fdr_significant({}) == frozenset()


@settings(max_examples=40, deadline=None)
@given(
    raw=st.floats(-1, 5),
    n=st.integers(0, 10_000),
    prior=st.floats(-1, 5),
    strength=st.floats(0.1, 500),
)
def test_shrink_lies_between_raw_and_prior(
    raw: float, n: int, prior: float, strength: float
) -> None:
    value = shrink(raw, n, prior, strength)
    assert min(raw, prior) - 1e-9 <= value <= max(raw, prior) + 1e-9


@settings(max_examples=25, deadline=None)
@given(outcomes=st.lists(st.tuples(st.booleans(), st.floats(1.1, 6.0)), min_size=1, max_size=40))
def test_interval_is_ordered_for_any_history(outcomes: list[tuple[bool, float]]) -> None:
    rows = [
        SegmentObservation(KEY, (odds - 1.0) if hit else -1.0, hit, 0.5, block=index // 3)
        for index, (hit, odds) in enumerate(outcomes)
    ]
    roi = stats_of(rows).roi
    assert roi.low is not None
    assert roi.high is not None
    assert roi.low <= roi.high
    assert roi.n == len(rows)
