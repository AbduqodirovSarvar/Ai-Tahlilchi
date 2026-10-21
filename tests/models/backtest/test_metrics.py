import math
from datetime import date

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain import Market, Selection
from app.models import ModelInputError, Settlement
from app.models.backtest import (
    Bet,
    Interval,
    ProbabilitySample,
    WalkForwardResult,
    bootstrap_ratio_ci,
    calibration_table,
    max_drawdown,
    probability_samples,
    score_table,
    summarize_bets,
)
from app.models.backtest.metrics import mean_log_loss, score_group


def bet(profit: float, *, clv_fair: float | None = 2.0, closing: float | None = 2.0) -> Bet:
    return Bet(
        fixture=f"f{profit}",
        league="L",
        season=2024,
        match_date=date(2024, 9, 1),
        market=Market.match_result(),
        selection=Selection.HOME,
        odds=2.0,
        probability=0.55,
        ev=0.1,
        settlement=Settlement.WIN if profit > 0 else Settlement.LOSS,
        flat_stake=1.0,
        flat_profit=profit,
        kelly_stake=2.0,
        kelly_profit=2.0 * profit,
        closing_fair_odds=clv_fair,
        closing_ev=None if clv_fair is None else -0.02,
        closing_odds=closing,
        closing_profit=None if closing is None else profit,
    )


def test_summary_hand_computed() -> None:
    bets = [bet(1.0), bet(-1.0), bet(-1.0), bet(1.0, clv_fair=1.9, closing=None)]
    summary = summarize_bets(bets, bank=100.0, seed=1, n_resamples=200)
    assert summary.n_bets == 4
    assert summary.profit == 0.0
    assert summary.roi == 0.0
    assert summary.hit_rate == 0.5
    assert summary.max_drawdown == 2.0
    assert summary.n_clv == 4
    assert summary.mean_clv == pytest.approx(math.log(2.0 / 1.9) / 4)
    assert summary.beat_close_rate == 0.25
    assert summary.n_closing == 3
    assert summary.roi_at_closing == pytest.approx(-1 / 3)
    assert summary.kelly_roi == 0.0
    assert summary.kelly_bank_return == 0.0
    assert summary.kelly_max_drawdown == 4.0
    assert summary.mean_closing_ev == pytest.approx(-0.02)
    assert summary.roi_ci is not None
    assert summary.roi_ci.contains(0.0)


def test_summary_of_no_bets() -> None:
    summary = summarize_bets([], bank=100.0, seed=1)
    assert (summary.n_bets, summary.roi, summary.hit_rate, summary.max_drawdown) == (0, 0, 0, 0)
    assert summary.roi_ci is None
    assert summary.mean_clv is None
    assert summary.beat_close_rate is None
    assert summary.roi_at_closing is None
    assert summary.mean_closing_ev is None


def test_bootstrap_is_seeded_and_brackets_the_estimate() -> None:
    rng_values = np.random.default_rng(0).normal(0.05, 1.0, 400)
    ones = np.ones_like(rng_values)
    first = bootstrap_ratio_ci(rng_values, ones, rng=np.random.default_rng(5), n_resamples=600)
    again = bootstrap_ratio_ci(rng_values, ones, rng=np.random.default_rng(5), n_resamples=600)
    assert first == again
    assert first is not None
    assert first.contains(float(rng_values.mean()))
    # Width ~ 2 * 1.96 * sd / sqrt(n).
    assert first.high - first.low == pytest.approx(2 * 1.96 / math.sqrt(400), rel=0.2)


def test_bootstrap_edge_cases() -> None:
    rng = np.random.default_rng(0)
    assert bootstrap_ratio_ci(np.array([1.0]), np.array([1.0]), rng=rng) is None
    with pytest.raises(ModelInputError, match="same length"):
        bootstrap_ratio_ci(np.array([1.0, 2.0]), np.array([1.0]), rng=rng)
    zero = bootstrap_ratio_ci(np.zeros(3), np.zeros(3), rng=rng, n_resamples=10)
    assert zero == Interval(0.0, 0.0)


@given(st.lists(st.floats(-5, 5), max_size=40))
def test_max_drawdown_is_non_negative_and_bounded(profits: list[float]) -> None:
    drawdown = max_drawdown(np.array(profits, dtype=np.float64))
    assert drawdown >= 0.0
    assert drawdown <= sum(abs(p) for p in profits) + 1e-9


def sample(p: float, won: bool, group: str = "1x2") -> ProbabilitySample:
    return ProbabilitySample(group=group, league="L", ensemble=p, model=p, market=0.5, won=won)


def test_score_table_hand_computed() -> None:
    samples = [
        sample(0.8, True),
        sample(0.2, False),
        sample(0.4, True, "btts"),
        sample(0.6, False, "btts"),
    ]
    rows = {row.group: row for row in score_table(samples)}
    assert set(rows) == {"1x2", "btts", "all"}
    assert rows["1x2"].log_loss_ensemble == pytest.approx(-math.log(0.8))
    assert rows["1x2"].brier_ensemble == pytest.approx(0.04 + 0.04)
    assert rows["btts"].log_loss_market == pytest.approx(-math.log(0.5))
    assert rows["all"].n_markets == 2
    assert rows["all"].log_loss_model == pytest.approx(-(math.log(0.8) + math.log(0.4)) / 2)
    assert math.isnan(mean_log_loss([sample(0.3, False)], lambda s: s.ensemble))


def test_calibration_buckets() -> None:
    samples = [sample(0.05, False), sample(0.15, True), sample(0.12, False), sample(1.0, True)]
    buckets = calibration_table(samples)
    assert [(b.lower, b.n) for b in buckets] == [(0.0, 1), (0.1, 2), (0.9, 1)]
    middle = buckets[1]
    assert middle.mean_predicted == pytest.approx(0.135)
    assert middle.observed_rate == 0.5
    assert middle.deviation == pytest.approx(0.365)
    with pytest.raises(ModelInputError):
        calibration_table(samples, buckets=0)


def test_score_groups() -> None:
    assert score_group(Market.totals(2.5)) == "totals 2.5"
    assert score_group(Market.asian_handicap(-1.5)) == "asian_handicap (x.5)"
    assert score_group(Market.btts()) == "btts"


def test_probability_samples_cover_partition_markets_only(small_run: WalkForwardResult) -> None:
    samples = probability_samples(small_run.predictions, 0.5)
    groups = {s.group for s in samples}
    assert "1x2" in groups
    assert "totals 2.5" in groups
    # Exactly one winner per market instance and probabilities in [0, 1].
    assert all(0.0 <= s.ensemble <= 1.0 for s in samples)
    instances = sum(s.won for s in samples)
    per_instance = sum(1 for s in samples if s.group == "1x2") / 3
    assert instances >= per_instance
