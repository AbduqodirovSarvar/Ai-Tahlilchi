"""Parameter selection on a validation season and a single hold-out evaluation.

Seasons are split into train (model warm-up history only), validation (the grid is scored
here) and hold-out (the last season, evaluated **once** with the chosen configuration).

Selection is two-stage to limit overfitting:

1. ``(xi, w)`` minimise the validation log-loss of the ensemble on partition markets (a
   proper scoring rule, not ROI);
2. with those fixed, ``(min_ev, min_probability)`` maximise validation ROI among the
   configurations with at least ``min_validation_bets`` bets.

Every configuration of the full grid is evaluated on validation and reported, together with
their count, so the reader can judge the multiple-testing optimism of the chosen one.
"""

import itertools
import math
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from app.domain import MarketType
from app.models.backtest.betting import (
    DEFAULT_ODDS_MAX,
    DEFAULT_ODDS_MIN,
    Bet,
    BetFilter,
    PricingConfig,
    StakingConfig,
    candidate_bets,
    select_bets,
)
from app.models.backtest.dataset import BacktestFixture
from app.models.backtest.metrics import (
    DEFAULT_RESAMPLES,
    BettingSummary,
    CalibrationBucket,
    ProbabilitySample,
    ScoreRow,
    calibration_table,
    mean_log_loss,
    probability_samples,
    score_table,
    summarize_bets,
)
from app.models.backtest.walk_forward import (
    DEFAULT_MAX_HISTORY_DAYS,
    DEFAULT_REFIT_DAYS,
    SkipReason,
    WalkForwardConfig,
    WalkForwardResult,
    walk_forward,
)
from app.models.exceptions import ModelInputError
from app.models.market import DEFAULT_MARKET_MODEL, MarketModelConfig

DEFAULT_SEED: Final = 20261021
DEFAULT_MIN_VALIDATION_BETS: Final = 50
# The ensemble with this weight is the de-vigged bet-time market alone.
PURE_MARKET_WEIGHT: Final = 0.0
_MIN_SEASONS: Final = 3


@dataclass(frozen=True, slots=True, kw_only=True)
class GridSpec:
    """Values tried for each tuned parameter."""

    xis: tuple[float, ...] = (0.0, 0.0019, 0.0065)
    # w = 0 is the pure-market baseline (decision #11); w = 1 the pure Dixon-Coles model.
    weights: tuple[float, ...] = (0.0, 0.25, 0.5, 0.75, 1.0)
    min_evs: tuple[float, ...] = (0.0, 0.02, 0.05, 0.08)
    min_probabilities: tuple[float, ...] = (0.30, 0.45, 0.55)

    def __post_init__(self) -> None:
        """Every axis needs at least one value."""
        if not all((self.xis, self.weights, self.min_evs, self.min_probabilities)):
            msg = f"every grid axis needs at least one value: {self}"
            raise ModelInputError(msg)

    @property
    def size(self) -> int:
        """Number of configurations in the grid."""
        return len(self.xis) * len(self.weights) * len(self.min_evs) * len(self.min_probabilities)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExperimentConfig:
    """Everything that defines a backtest run (recorded in the report)."""

    grid: GridSpec = field(default_factory=GridSpec)
    refit_days: int = DEFAULT_REFIT_DAYS
    max_history_days: int = DEFAULT_MAX_HISTORY_DAYS
    odds_min: float = DEFAULT_ODDS_MIN
    odds_max: float = DEFAULT_ODDS_MAX
    pricing: PricingConfig = field(default_factory=PricingConfig)
    staking: StakingConfig = field(default_factory=StakingConfig)
    market_model: MarketModelConfig = DEFAULT_MARKET_MODEL
    seed: int = DEFAULT_SEED
    n_resamples: int = DEFAULT_RESAMPLES
    min_validation_bets: int = DEFAULT_MIN_VALIDATION_BETS
    holdout_season: int | None = None

    def walk_forward_config(self, xi: float) -> WalkForwardConfig:
        """Walk-forward settings for one ``xi``."""
        return WalkForwardConfig(
            xi=xi,
            refit_days=self.refit_days,
            max_history_days=self.max_history_days,
            prediction_kind=self.pricing.kind,
            market_model=self.market_model,
        )

    def bet_filter(self, min_ev: float, min_probability: float) -> BetFilter:
        """Bet filter with the fixed odds range and the given thresholds."""
        return BetFilter(
            min_probability=min_probability,
            min_ev=min_ev,
            odds_min=self.odds_min,
            odds_max=self.odds_max,
        )


@dataclass(frozen=True, slots=True)
class SeasonSplit:
    """Train (warm-up), validation and hold-out seasons."""

    train: tuple[int, ...]
    validation: int
    holdout: int


def split_seasons(seasons: Collection[int], holdout: int | None = None) -> SeasonSplit:
    """Hold-out = ``holdout`` (default: the last season), validation = the one before it.

    Raises:
        ModelInputError: fewer than three seasons up to the hold-out, or an unknown one.
    """
    ordered = sorted(set(seasons))
    chosen = ordered[-1] if holdout is None and ordered else holdout
    if chosen not in ordered:
        msg = f"hold-out season {chosen} is not in the data {ordered}"
        raise ModelInputError(msg)
    usable = ordered[: ordered.index(chosen) + 1]
    if len(usable) < _MIN_SEASONS:
        msg = f"need >= {_MIN_SEASONS} seasons (train, validation, hold-out), got {usable}"
        raise ModelInputError(msg)
    return SeasonSplit(train=tuple(usable[:-2]), validation=usable[-2], holdout=usable[-1])


@dataclass(frozen=True, slots=True, kw_only=True)
class GridRow:
    """Validation result of one configuration."""

    xi: float
    weight: float
    min_ev: float
    min_probability: float
    n_bets: int
    roi: float
    log_loss: float


@dataclass(frozen=True, slots=True, kw_only=True)
class ChosenParameters:
    """The configuration evaluated on the hold-out season."""

    xi: float
    weight: float
    min_ev: float
    min_probability: float
    underpowered: bool
    boundary_weight: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class Evaluation:
    """Results of one configuration on one season.

    ``log_loss`` is the ensemble's mean log-loss over every partition market and
    ``baseline_log_loss`` the same for the pure bet-time market (``w = 0``).
    """

    season: int
    summary: BettingSummary
    scores: tuple[ScoreRow, ...]
    calibration: tuple[CalibrationBucket, ...]
    by_league: Mapping[str, BettingSummary]
    by_market: Mapping[str, BettingSummary]
    bets: tuple[Bet, ...]
    log_loss: float
    baseline_log_loss: float
    predictions: int
    skipped: Mapping[SkipReason, int]
    fits: int
    fit_failures: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ExperimentResult:
    """Grid, chosen configuration and its single hold-out evaluation."""

    split: SeasonSplit
    config: ExperimentConfig
    rows: tuple[GridRow, ...]
    chosen: ChosenParameters
    validation: Evaluation
    holdout: Evaluation

    @property
    def n_configurations(self) -> int:
        """How many configurations were tried on validation (multiple-testing count)."""
        return len(self.rows)


def run_experiment(
    dataset: Sequence[BacktestFixture], config: ExperimentConfig
) -> ExperimentResult:
    """Tune on validation, then evaluate the chosen configuration once on hold-out."""
    split = split_seasons({item.season for item in dataset}, config.holdout_season)
    runs = {
        xi: walk_forward(dataset, config.walk_forward_config(xi), seasons={split.validation})
        for xi in config.grid.xis
    }
    rows = _grid_rows(runs, config)
    chosen = _choose(rows, config)
    validation = evaluate(runs[chosen.xi], chosen, config, season=split.validation)
    holdout_run = walk_forward(
        dataset, config.walk_forward_config(chosen.xi), seasons={split.holdout}
    )
    holdout = evaluate(holdout_run, chosen, config, season=split.holdout)
    return ExperimentResult(
        split=split,
        config=config,
        rows=tuple(rows),
        chosen=chosen,
        validation=validation,
        holdout=holdout,
    )


def evaluate(
    run: WalkForwardResult, chosen: ChosenParameters, config: ExperimentConfig, *, season: int
) -> Evaluation:
    """Bets, returns, scores and calibration of one configuration on one walk-forward run."""
    candidates = candidate_bets(
        run.predictions, weight=chosen.weight, pricing=config.pricing, staking=config.staking
    )
    bets = select_bets(candidates, config.bet_filter(chosen.min_ev, chosen.min_probability))
    samples = probability_samples(run.predictions, chosen.weight)
    baseline = probability_samples(run.predictions, PURE_MARKET_WEIGHT)

    def summary(selected: Sequence[Bet]) -> BettingSummary:
        return summarize_bets(
            selected, bank=config.staking.bank, seed=config.seed, n_resamples=config.n_resamples
        )

    return Evaluation(
        season=season,
        summary=summary(bets),
        scores=tuple(score_table(samples)),
        calibration=tuple(calibration_table(samples)),
        by_league={name: summary(group) for name, group in _group(bets, _league_of).items()},
        by_market={name: summary(group) for name, group in _group(bets, _market_of).items()},
        bets=tuple(bets),
        log_loss=mean_log_loss(samples, _ensemble_forecast),
        baseline_log_loss=mean_log_loss(baseline, _ensemble_forecast),
        predictions=len(run.predictions),
        skipped=run.skipped,
        fits=run.fits,
        fit_failures=run.fit_failures,
    )


def _grid_rows(runs: Mapping[float, WalkForwardResult], config: ExperimentConfig) -> list[GridRow]:
    grid = config.grid
    rows = []
    for xi, weight in itertools.product(grid.xis, grid.weights):
        predictions = runs[xi].predictions
        log_loss = mean_log_loss(probability_samples(predictions, weight), _ensemble_forecast)
        candidates = candidate_bets(
            predictions, weight=weight, pricing=config.pricing, staking=config.staking
        )
        for min_ev, min_probability in itertools.product(grid.min_evs, grid.min_probabilities):
            bets = select_bets(candidates, config.bet_filter(min_ev, min_probability))
            stake = math.fsum(bet.flat_at_risk for bet in bets)
            profit = math.fsum(bet.flat_profit for bet in bets)
            rows.append(
                GridRow(
                    xi=xi,
                    weight=weight,
                    min_ev=min_ev,
                    min_probability=min_probability,
                    n_bets=len(bets),
                    roi=profit / stake if stake > 0 else 0.0,
                    log_loss=log_loss,
                )
            )
    return rows


def _choose(rows: Sequence[GridRow], config: ExperimentConfig) -> ChosenParameters:
    """Stage 1: (xi, w) by log-loss; stage 2: thresholds by ROI with enough bets."""
    scored = [row for row in rows if math.isfinite(row.log_loss)]
    if not scored:
        msg = "no validation predictions could be scored; check data coverage"
        raise ModelInputError(msg)
    best_fit = min(scored, key=lambda row: row.log_loss)
    stage_two = [row for row in rows if row.xi == best_fit.xi and row.weight == best_fit.weight]
    powered = [row for row in stage_two if row.n_bets >= config.min_validation_bets]
    best = (
        max(powered, key=lambda row: row.roi)
        if powered
        else max(stage_two, key=lambda row: row.n_bets)
    )
    return ChosenParameters(
        xi=best.xi,
        weight=best.weight,
        min_ev=best.min_ev,
        min_probability=best.min_probability,
        underpowered=not powered,
        boundary_weight=best.weight in {min(config.grid.weights), max(config.grid.weights)},
    )


def _ensemble_forecast(sample: ProbabilitySample) -> float:
    return sample.ensemble


def _group(bets: Sequence[Bet], key: Callable[[Bet], str]) -> dict[str, list[Bet]]:
    groups: dict[str, list[Bet]] = {}
    for bet in bets:
        groups.setdefault(key(bet), []).append(bet)
    return dict(sorted(groups.items()))


def _league_of(bet: Bet) -> str:
    return bet.league


def _market_of(bet: Bet) -> str:
    """Totals per line; Asian handicaps together (their lines vary match by match)."""
    if bet.market.type is MarketType.TOTALS:
        return str(bet.market)
    return bet.market.type.value
