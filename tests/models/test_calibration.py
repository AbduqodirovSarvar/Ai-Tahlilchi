"""Isotonic calibration: fit, hold-out gate, leakage safety, complementary outcomes (F8.6)."""

import math
import random
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.models.asian import AsianOutcomeProbabilities
from app.models.calibration import (
    CalibrationPolicy,
    CalibrationReason,
    CalibrationSample,
    CalibrationVersion,
    apply_calibration,
    calibrate_complementary,
    fit_calibration,
    fit_isotonic,
    rescale_outcomes,
    select_rollback,
)
from app.models.exceptions import ModelInputError

START = datetime(2026, 1, 1, tzinfo=UTC)
CUTOFF = datetime(2027, 1, 1, tzinfo=UTC)
POLICY = CalibrationPolicy(resamples=400)


def history(n: int, *, true_shift: float, seed: int = 1) -> list[CalibrationSample]:
    """Predictions in 0.45..0.75; the true hit rate is the prediction minus ``true_shift``."""
    rng = random.Random(seed)  # noqa: S311 - simulation, not security
    samples = []
    for index in range(n):
        p = rng.uniform(0.45, 0.75)
        hit = rng.random() < p - true_shift
        samples.append(CalibrationSample(p, hit, START + timedelta(hours=index)))
    return samples


def version_of(x: list[float], y: list[float], number: int = 1, *, ok: bool = True):
    return CalibrationVersion(
        version=number,
        method="isotonic",
        params={"x": x, "y": y},
        n_samples=300,
        brier_before=0.25,
        brier_after=0.24,
        created_at=START,
        passed_gate=ok,
    )


def test_isotonic_fit_is_monotone_and_pools_violations() -> None:
    params = fit_isotonic([0.1, 0.2, 0.3, 0.4], [True, False, True, False])
    assert params["y"] == sorted(params["y"])
    assert params["x"] == sorted(params["x"])
    # hit, miss pool to 0.5; hit, miss pool to 0.5; equal neighbours stay separate knots
    assert params["y"] == pytest.approx([0.5, 0.5])
    assert params["x"] == pytest.approx([0.15, 0.35])


def test_isotonic_hand_example() -> None:
    # p=0.2 -> 0, p=0.4 -> 1, p=0.6 -> 0: the last two violate and pool to 0.5 at p=0.5
    params = fit_isotonic([0.2, 0.4, 0.6], [False, True, False])
    assert params["x"] == pytest.approx([0.2, 0.5])
    assert params["y"] == pytest.approx([0.0, 0.5])


def test_isotonic_pools_equal_probabilities() -> None:
    params = fit_isotonic([0.5, 0.5, 0.5, 0.5], [True, True, False, False])
    assert params == {"x": [0.5], "y": [0.5]}


@pytest.mark.parametrize(
    ("probabilities", "hits"), [([], []), ([0.5], [True, False]), ([1.2], [True])]
)
def test_isotonic_rejects_bad_input(probabilities: list[float], hits: list[bool]) -> None:
    with pytest.raises(ModelInputError):
        fit_isotonic(probabilities, hits)


def test_apply_clamps_to_band_and_rejects_invalid() -> None:
    version = version_of([0.3, 0.7], [0.0, 1.0])
    assert apply_calibration(version, 0.0) == pytest.approx(0.01)
    assert apply_calibration(version, 1.0) == pytest.approx(0.99)
    assert apply_calibration(version, 0.5) == pytest.approx(0.5)
    with pytest.raises(ModelInputError):
        apply_calibration(version, float("nan"))
    with pytest.raises(ModelInputError):
        apply_calibration(version, 1.5)
    with pytest.raises(ModelInputError):
        apply_calibration(
            CalibrationVersion(1, "platt", {}, 1, 0.1, 0.1, START, passed_gate=True), 0.5
        )


def test_complementary_outcomes_sum_to_one() -> None:
    version = version_of([0.2, 0.5, 0.8], [0.15, 0.4, 0.7])
    calibrated = calibrate_complementary(version, [0.5, 0.3, 0.2])
    assert math.fsum(calibrated) == pytest.approx(1.0)
    assert calibrated[0] > calibrated[1] > calibrated[2]
    with pytest.raises(ModelInputError):
        calibrate_complementary(version, [0.5, 0.3])


def test_miscalibrated_model_is_calibrated_and_brier_improves() -> None:
    fit = fit_calibration(history(4000, true_shift=0.08), cutoff=CUTOFF, version=1, policy=POLICY)
    assert fit.reason is CalibrationReason.IMPROVED
    assert fit.activate
    assert fit.version is not None
    assert fit.version.brier_after < fit.version.brier_before
    assert fit.gain_low is not None
    assert fit.gain_low > 0.0
    # the map pulls a stated 60% down to about 52%
    assert apply_calibration(fit.version, 0.60) == pytest.approx(0.52, abs=0.04)
    assert fit.n_train + fit.n_holdout == fit.version.n_samples == 4000


def test_well_calibrated_model_is_not_activated() -> None:
    for seed in range(5):
        fit = fit_calibration(
            history(2000, true_shift=0.0, seed=seed), cutoff=CUTOFF, version=1, policy=POLICY
        )
        assert not fit.activate, seed
        assert fit.version is not None
        assert fit.version.passed_gate is False


def test_small_miscalibrated_sample_is_not_significant() -> None:
    fit = fit_calibration(history(250, true_shift=0.08), cutoff=CUTOFF, version=1, policy=POLICY)
    assert not fit.activate
    assert fit.reason in {CalibrationReason.NOT_SIGNIFICANT, CalibrationReason.NO_IMPROVEMENT}


def test_too_few_samples() -> None:
    fit = fit_calibration(history(199, true_shift=0.1), cutoff=CUTOFF, version=1, policy=POLICY)
    assert fit.reason is CalibrationReason.TOO_FEW_SAMPLES
    assert fit.version is None
    assert not fit.activate


def test_no_leakage_future_samples_do_not_change_an_earlier_fit() -> None:
    samples = history(1500, true_shift=0.08)
    cutoff = START + timedelta(hours=1000)
    baseline = fit_calibration(samples, cutoff=cutoff, version=1, policy=POLICY)
    perturbed = [
        s
        if s.settled_at < cutoff
        else CalibrationSample(1.0 - s.probability, not s.hit, s.settled_at)
        for s in samples
    ]
    extended = [*perturbed, CalibrationSample(0.5, True, cutoff)]
    assert fit_calibration(extended, cutoff=cutoff, version=1, policy=POLICY) == baseline


def test_fit_does_not_depend_on_input_order() -> None:
    samples = history(1200, true_shift=0.08)
    shuffled = list(samples)
    random.Random(3).shuffle(shuffled)  # noqa: S311 - simulation
    assert fit_calibration(shuffled, cutoff=CUTOFF, version=1, policy=POLICY) == fit_calibration(
        samples, cutoff=CUTOFF, version=1, policy=POLICY
    )


def test_holdout_is_the_latest_part() -> None:
    fit = fit_calibration(history(1000, true_shift=0.0), cutoff=CUTOFF, version=1, policy=POLICY)
    assert fit.n_holdout == 300
    assert fit.n_train == 700


def test_rescale_outcomes_binary_and_asian() -> None:
    binary = AsianOutcomeProbabilities.binary(0.6)
    scaled = rescale_outcomes(binary, 0.52)
    assert scaled.win == pytest.approx(0.52)
    assert scaled.loss == pytest.approx(0.48)
    asian = AsianOutcomeProbabilities(0.3, 0.2, 0.1, 0.15, 0.25)
    shifted = rescale_outcomes(asian, 0.4)
    assert shifted.win + shifted.half_win == pytest.approx(0.4)
    assert math.fsum(shifted.as_tuple()) == pytest.approx(1.0)
    assert shifted.win / shifted.half_win == pytest.approx(0.3 / 0.2)
    assert shifted.push / shifted.loss == pytest.approx(0.1 / 0.25)


def test_rescale_outcomes_edges() -> None:
    sure = AsianOutcomeProbabilities.binary(1.0)
    lowered = rescale_outcomes(sure, 0.9)
    assert lowered.win == pytest.approx(0.9)
    assert lowered.loss == pytest.approx(0.1)
    never = AsianOutcomeProbabilities.binary(0.0)
    assert rescale_outcomes(never, 0.3) == never


def test_select_rollback_picks_previous_passed_version() -> None:
    v1, v2 = version_of([0.5], [0.5], 1), version_of([0.5], [0.4], 2, ok=False)
    v3 = version_of([0.5], [0.45], 3)
    assert select_rollback([v1, v2, v3], v3) == v1
    assert select_rollback([v1, v2, v3], v1) is None


@settings(max_examples=60, deadline=None)
@given(
    data=st.lists(st.tuples(st.floats(0, 1), st.booleans()), min_size=1, max_size=80),
    query=st.floats(0, 1),
)
def test_fit_is_monotone_and_in_range(data: list[tuple[float, bool]], query: float) -> None:
    params = fit_isotonic([p for p, _ in data], [h for _, h in data])
    assert all(a <= b + 1e-12 for a, b in zip(params["y"], params["y"][1:], strict=False))
    assert all(a < b for a, b in zip(params["x"], params["x"][1:], strict=False))
    version = version_of(params["x"], params["y"])
    assert 0.0 < apply_calibration(version, query) < 1.0


@settings(max_examples=60, deadline=None)
@given(
    data=st.lists(st.tuples(st.floats(0, 1), st.booleans()), min_size=2, max_size=60),
    pair=st.tuples(st.floats(0, 1), st.floats(0, 1)),
)
def test_apply_is_monotone(data: list[tuple[float, bool]], pair: tuple[float, float]) -> None:
    params = fit_isotonic([p for p, _ in data], [h for _, h in data])
    version = version_of(params["x"], params["y"])
    low, high = sorted(pair)
    assert apply_calibration(version, low) <= apply_calibration(version, high) + 1e-12


@settings(max_examples=60, deadline=None)
@given(
    raw=st.lists(st.floats(0.01, 1.0), min_size=2, max_size=5),
)
def test_complementary_always_renormalises(raw: list[float]) -> None:
    total = sum(raw)
    probabilities = [value / total for value in raw]
    version = version_of([0.1, 0.5, 0.9], [0.2, 0.45, 0.7])
    out = calibrate_complementary(version, probabilities)
    assert math.fsum(out) == pytest.approx(1.0)
    assert np.all(np.array(out) > 0)
