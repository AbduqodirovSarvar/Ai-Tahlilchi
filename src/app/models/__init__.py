"""Pure statistical models: probabilities, de-vig, EV and staking (no I/O).

This module is the public model API used by the pipeline (contract 6 in docs/contracts.md).
Functions are synchronous and CPU-bound; async callers run them via ``asyncio.to_thread``.
"""

from app.models.asian import (
    AsianOutcomeProbabilities,
    Settlement,
    is_quarter_line,
    settle,
    split_line,
)
from app.models.devig import (
    DevigMethod,
    devig,
    devig_power,
    devig_proportional,
    devig_shin,
    implied_probabilities,
    overround,
    shin_insider_share,
)
from app.models.ev import asian_expected_value, expected_value, fair_odds
from app.models.exceptions import ModelFitError, ModelInputError, UnknownTeamError
from app.models.poisson import (
    DEFAULT_XI_PER_DAY,
    DixonColesModel,
    ExpectedGoals,
    MatchRecord,
    feasible_rho,
    fit_dixon_coles,
    score_matrix,
)
from app.models.score_markets import (
    BothTeamsToScoreProbabilities,
    DoubleChanceProbabilities,
    MatchResultProbabilities,
    ScoreProbability,
    TwoSidedAsianProbabilities,
    asian_handicap,
    both_teams_to_score,
    double_chance,
    match_result,
    top_scores,
    total_goals,
)
from app.models.staking import (
    DEFAULT_KELLY_MULTIPLIER,
    DEFAULT_MAX_STAKE_FRACTION,
    asian_kelly_fraction,
    capped_fraction,
    flat_stake,
    kelly_fraction,
    stake_amount,
)

__all__ = [
    "DEFAULT_KELLY_MULTIPLIER",
    "DEFAULT_MAX_STAKE_FRACTION",
    "DEFAULT_XI_PER_DAY",
    "AsianOutcomeProbabilities",
    "BothTeamsToScoreProbabilities",
    "DevigMethod",
    "DixonColesModel",
    "DoubleChanceProbabilities",
    "ExpectedGoals",
    "MatchRecord",
    "MatchResultProbabilities",
    "ModelFitError",
    "ModelInputError",
    "ScoreProbability",
    "Settlement",
    "TwoSidedAsianProbabilities",
    "UnknownTeamError",
    "asian_expected_value",
    "asian_handicap",
    "asian_kelly_fraction",
    "both_teams_to_score",
    "capped_fraction",
    "devig",
    "devig_power",
    "devig_proportional",
    "devig_shin",
    "double_chance",
    "expected_value",
    "fair_odds",
    "feasible_rho",
    "fit_dixon_coles",
    "flat_stake",
    "implied_probabilities",
    "is_quarter_line",
    "kelly_fraction",
    "match_result",
    "overround",
    "score_matrix",
    "settle",
    "shin_insider_share",
    "split_line",
    "stake_amount",
    "top_scores",
    "total_goals",
]
