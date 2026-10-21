"""Backtest metrics: betting returns with bootstrap CIs, proper scores and calibration.

Accuracy and win rate are deliberately absent: they reward picking favourites, not good
probabilities. Probabilities are judged by proper scoring rules (log-loss, Brier) against
the de-vigged closing line, the hardest public baseline.
"""

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Final

import numpy as np
import numpy.typing as npt

from app.domain import Market, MarketType
from app.models.asian import Settlement
from app.models.backtest.betting import Bet
from app.models.backtest.walk_forward import FixturePrediction
from app.models.ensemble import ensemble
from app.models.exceptions import ModelInputError
from app.models.outcomes import is_partition_market, settle_selection

type FloatArray = npt.NDArray[np.float64]

DEFAULT_RESAMPLES: Final = 2000
CONFIDENCE_LEVEL: Final = 0.95
DEFAULT_CALIBRATION_BUCKETS: Final = 10
# Probabilities are clipped here before taking logs (a 0 would give an infinite loss).
LOG_LOSS_EPSILON: Final = 1e-15
# Resamples are drawn in chunks to bound memory (chunk x n_bets indices).
_BOOTSTRAP_CHUNK: Final = 250


@dataclass(frozen=True, slots=True)
class Interval:
    """A two-sided confidence interval."""

    low: float
    high: float

    def contains(self, value: float) -> bool:
        """True if ``value`` lies inside the interval (bounds included)."""
        return self.low <= value <= self.high


@dataclass(frozen=True, slots=True, kw_only=True)
class BettingSummary:
    """Returns of a set of bets under flat and fractional-Kelly staking.

    ROI is profit per unit staked (also called yield). ``kelly_bank_return`` is the Kelly
    profit relative to the starting bank. CLV statistics cover only bets whose market has
    a closing price (an Asian line can move between opening and closing, leaving the bet
    without one); ``n_without_closing`` and ``roi_without_closing`` (at the odds taken)
    describe the excluded bets so the coverage is visible. ``roi_at_closing`` re-prices
    the bets that have a closing quote at the bookmaker's closing odds.
    """

    n_bets: int
    turnover: float
    profit: float
    roi: float
    roi_ci: Interval | None
    hit_rate: float
    max_drawdown: float
    n_clv: int
    n_without_closing: int
    roi_without_closing: float | None
    mean_clv: float | None
    clv_ci: Interval | None
    beat_close_rate: float | None
    mean_closing_ev: float | None
    n_closing: int
    roi_at_closing: float | None
    roi_at_closing_ci: Interval | None
    kelly_turnover: float
    kelly_profit: float
    kelly_roi: float
    kelly_bank_return: float
    kelly_max_drawdown: float


def summarize_bets(
    bets: Sequence[Bet], *, bank: float, seed: int, n_resamples: int = DEFAULT_RESAMPLES
) -> BettingSummary:
    """Summary statistics; CIs are percentile bootstraps over bets (seeded)."""
    rng = np.random.default_rng(seed)
    flat_stake = np.array([bet.flat_stake for bet in bets], dtype=np.float64)
    flat_profit = np.array([bet.flat_profit for bet in bets], dtype=np.float64)
    kelly_stake = np.array([bet.kelly_stake for bet in bets], dtype=np.float64)
    kelly_profit = np.array([bet.kelly_profit for bet in bets], dtype=np.float64)
    clv = np.array([bet.clv for bet in bets if bet.clv is not None], dtype=np.float64)
    closing_ev = [bet.closing_ev for bet in bets if bet.closing_ev is not None]
    at_closing = [bet for bet in bets if bet.closing_profit is not None]
    closing_profit = np.array([bet.closing_profit for bet in at_closing], dtype=np.float64)
    closing_stake = np.array([bet.flat_stake for bet in at_closing], dtype=np.float64)
    without = [bet for bet in bets if bet.clv is None]
    return BettingSummary(
        n_bets=len(bets),
        turnover=float(flat_stake.sum()),
        profit=float(flat_profit.sum()),
        roi=_ratio(flat_profit, flat_stake),
        roi_ci=bootstrap_ratio_ci(flat_profit, flat_stake, rng=rng, n_resamples=n_resamples),
        hit_rate=float(np.mean(flat_profit > 0.0)) if bets else 0.0,
        max_drawdown=max_drawdown(flat_profit),
        n_clv=len(clv),
        n_without_closing=len(without),
        roi_without_closing=(
            math.fsum(b.flat_profit for b in without) / math.fsum(b.flat_stake for b in without)
            if without
            else None
        ),
        mean_clv=float(clv.mean()) if len(clv) else None,
        clv_ci=bootstrap_ratio_ci(clv, np.ones_like(clv), rng=rng, n_resamples=n_resamples),
        beat_close_rate=float(np.mean(clv > 0.0)) if len(clv) else None,
        mean_closing_ev=math.fsum(closing_ev) / len(closing_ev) if closing_ev else None,
        n_closing=len(at_closing),
        roi_at_closing=_ratio(closing_profit, closing_stake) if at_closing else None,
        roi_at_closing_ci=bootstrap_ratio_ci(
            closing_profit, closing_stake, rng=rng, n_resamples=n_resamples
        ),
        kelly_turnover=float(kelly_stake.sum()),
        kelly_profit=float(kelly_profit.sum()),
        kelly_roi=_ratio(kelly_profit, kelly_stake),
        kelly_bank_return=float(kelly_profit.sum()) / bank,
        kelly_max_drawdown=max_drawdown(kelly_profit),
    )


def bootstrap_ratio_ci(
    numerator: FloatArray,
    denominator: FloatArray,
    *,
    rng: np.random.Generator,
    n_resamples: int = DEFAULT_RESAMPLES,
) -> Interval | None:
    """95% percentile bootstrap CI of ``sum(numerator) / sum(denominator)``.

    Resampling is over items (bets), which are independent because there is at most one bet
    per fixture. ``None`` with fewer than two items.
    """
    n_items = len(numerator)
    if n_items != len(denominator):
        msg = "numerator and denominator must have the same length"
        raise ModelInputError(msg)
    if n_items < 2 or n_resamples < 1:  # noqa: PLR2004 - a CI needs at least two items
        return None
    estimates: list[FloatArray] = []
    remaining = n_resamples
    while remaining > 0:
        size = min(remaining, _BOOTSTRAP_CHUNK)
        index = rng.integers(0, n_items, size=(size, n_items))
        totals = denominator[index].sum(axis=1)
        estimates.append(np.divide(numerator[index].sum(axis=1), np.where(totals == 0, 1, totals)))
        remaining -= size
    values = np.concatenate(estimates)
    tail = (1.0 - CONFIDENCE_LEVEL) / 2.0 * 100.0
    low, high = np.percentile(values, [tail, 100.0 - tail])
    return Interval(float(low), float(high))


def max_drawdown(profits: FloatArray) -> float:
    """Largest peak-to-trough fall of the cumulative profit (starting from 0), >= 0."""
    if len(profits) == 0:
        return 0.0
    equity = np.concatenate([[0.0], np.cumsum(profits)])
    return float(np.max(np.maximum.accumulate(equity) - equity))


def _ratio(numerator: FloatArray, denominator: FloatArray) -> float:
    total = float(denominator.sum())
    return float(numerator.sum()) / total if total > 0 else 0.0


@dataclass(frozen=True, slots=True, kw_only=True)
class ProbabilitySample:
    """One selection of one partition market: three forecasts and what happened."""

    group: str
    league: str
    ensemble: float
    model: float
    market: float
    won: bool


def score_group(market: Market) -> str:
    """Reporting group of a market: totals per line, half-line handicaps together."""
    if market.type is MarketType.TOTALS:
        return str(market)
    if market.type is MarketType.ASIAN_HANDICAP:
        return "asian_handicap (x.5)"
    return market.type.value


def probability_samples(
    predictions: Iterable[FixturePrediction], weight: float
) -> list[ProbabilitySample]:
    """Samples of every partition market that has model, bet-time and closing estimates."""
    samples = []
    for prediction in predictions:
        record = prediction.item.record
        for market, model in prediction.model.items():
            at_bet_time = prediction.market.get(market)
            closing = prediction.closing.get(market)
            if not is_partition_market(market) or at_bet_time is None or closing is None:
                continue
            mixed = ensemble(model, at_bet_time.probabilities, weight)
            for selection in market.selections:
                result = settle_selection(market, selection, record.home_goals, record.away_goals)
                samples.append(
                    ProbabilitySample(
                        group=score_group(market),
                        league=prediction.item.league_name,
                        ensemble=mixed.win_probability(selection),
                        model=model.win_probability(selection),
                        market=closing.probabilities.win_probability(selection),
                        won=result is Settlement.WIN,
                    )
                )
    return samples


@dataclass(frozen=True, slots=True, kw_only=True)
class ScoreRow:
    """Mean log-loss and Brier score per market instance, for three forecasters."""

    group: str
    n_markets: int
    log_loss_ensemble: float
    log_loss_model: float
    log_loss_market: float
    brier_ensemble: float
    brier_model: float
    brier_market: float


def score_table(samples: Sequence[ProbabilitySample]) -> list[ScoreRow]:
    """One row per market group plus an ``all`` row (sorted by group name)."""
    groups: dict[str, list[ProbabilitySample]] = {}
    for sample in samples:
        groups.setdefault(sample.group, []).append(sample)
    rows = [_score_row(name, items) for name, items in sorted(groups.items())]
    if len(groups) > 1:
        rows.append(_score_row("all", samples))
    return rows


def mean_log_loss(
    samples: Sequence[ProbabilitySample], forecaster: Callable[[ProbabilitySample], float]
) -> float:
    """Mean log-loss per market instance (exactly one selection wins per instance)."""
    winners = [sample for sample in samples if sample.won]
    if not winners:
        return math.nan
    return -math.fsum(math.log(max(forecaster(s), LOG_LOSS_EPSILON)) for s in winners) / len(
        winners
    )


def _score_row(group: str, samples: Sequence[ProbabilitySample]) -> ScoreRow:
    n_markets = sum(sample.won for sample in samples)

    def brier(forecaster: Callable[[ProbabilitySample], float]) -> float:
        total = math.fsum((forecaster(s) - float(s.won)) ** 2 for s in samples)
        return total / n_markets if n_markets else math.nan

    return ScoreRow(
        group=group,
        n_markets=n_markets,
        log_loss_ensemble=mean_log_loss(samples, _ensemble),
        log_loss_model=mean_log_loss(samples, _model),
        log_loss_market=mean_log_loss(samples, _market),
        brier_ensemble=brier(_ensemble),
        brier_model=brier(_model),
        brier_market=brier(_market),
    )


def _ensemble(sample: ProbabilitySample) -> float:
    return sample.ensemble


def _model(sample: ProbabilitySample) -> float:
    return sample.model


def _market(sample: ProbabilitySample) -> float:
    return sample.market


@dataclass(frozen=True, slots=True, kw_only=True)
class CalibrationBucket:
    """Forecasts in ``[lower, upper)`` and how often those selections actually won."""

    lower: float
    upper: float
    n: int
    mean_predicted: float
    observed_rate: float

    @property
    def deviation(self) -> float:
        """``observed - predicted`` (positive: the model was too pessimistic)."""
        return self.observed_rate - self.mean_predicted


def calibration_table(
    samples: Sequence[ProbabilitySample], *, buckets: int = DEFAULT_CALIBRATION_BUCKETS
) -> list[CalibrationBucket]:
    """Calibration of the ensemble forecast in equal-width buckets (empty ones omitted)."""
    if buckets < 1:
        msg = f"buckets must be >= 1, got {buckets}"
        raise ModelInputError(msg)
    filled: dict[int, list[ProbabilitySample]] = {}
    for sample in samples:
        index = min(int(sample.ensemble * buckets), buckets - 1)
        filled.setdefault(index, []).append(sample)
    return [
        CalibrationBucket(
            lower=index / buckets,
            upper=(index + 1) / buckets,
            n=len(items),
            mean_predicted=math.fsum(item.ensemble for item in items) / len(items),
            observed_rate=sum(item.won for item in items) / len(items),
        )
        for index, items in sorted(filled.items())
    ]
