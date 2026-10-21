"""Walk-forward backtest library (F3.7): pure functions over in-memory data.

Callers (``scripts/backtest.py``) load the data, build a :class:`~app.domain.HistoryBatch`
and pass it in; this package never touches files, the database or the network.
Methodology: docs/adr/0005-backtest-methodology.md.
"""

from app.models.backtest.betting import (
    Bet,
    BetFilter,
    PricingConfig,
    StakingConfig,
    candidate_bets,
    select_bets,
)
from app.models.backtest.dataset import (
    BacktestFixture,
    build_dataset,
    complete_seasons,
    dataset_fingerprint,
    season_end,
)
from app.models.backtest.experiment import (
    ChosenParameters,
    Evaluation,
    ExperimentConfig,
    ExperimentResult,
    GridRow,
    GridSpec,
    SeasonSplit,
    evaluate,
    run_experiment,
    split_seasons,
)
from app.models.backtest.metrics import (
    BettingSummary,
    CalibrationBucket,
    Interval,
    ProbabilitySample,
    ScoreRow,
    bootstrap_ratio_ci,
    calibration_table,
    max_drawdown,
    probability_samples,
    score_table,
    summarize_bets,
)
from app.models.backtest.report import (
    GateCheck,
    GateStatus,
    RunMetadata,
    gate_checks,
    gate_passed,
    render_report,
)
from app.models.backtest.synthetic import SyntheticConfig, generate_history
from app.models.backtest.walk_forward import (
    FixturePrediction,
    SkipReason,
    WalkForwardConfig,
    WalkForwardResult,
    walk_forward,
)

__all__ = [
    "BacktestFixture",
    "Bet",
    "BetFilter",
    "BettingSummary",
    "CalibrationBucket",
    "ChosenParameters",
    "Evaluation",
    "ExperimentConfig",
    "ExperimentResult",
    "FixturePrediction",
    "GateCheck",
    "GateStatus",
    "GridRow",
    "GridSpec",
    "Interval",
    "PricingConfig",
    "ProbabilitySample",
    "RunMetadata",
    "ScoreRow",
    "SeasonSplit",
    "SkipReason",
    "StakingConfig",
    "SyntheticConfig",
    "WalkForwardConfig",
    "WalkForwardResult",
    "bootstrap_ratio_ci",
    "build_dataset",
    "calibration_table",
    "candidate_bets",
    "complete_seasons",
    "dataset_fingerprint",
    "evaluate",
    "gate_checks",
    "gate_passed",
    "generate_history",
    "max_drawdown",
    "probability_samples",
    "render_report",
    "run_experiment",
    "score_table",
    "season_end",
    "select_bets",
    "split_seasons",
    "summarize_bets",
    "walk_forward",
]
