"""Tunable numbers of the pipeline (nothing here is read from the environment)."""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final

from app.domain import BOOKMAKER_1XBET
from app.errors import ConfigurationError
from app.models import DEFAULT_MODEL_WEIGHT, DEFAULT_XI_PER_DAY
from app.models.confidence import MIN_TEAM_MATCHES
from app.pipeline.review import DEFAULT_REVIEW_TIMEOUT

HISTORY_DAYS: Final = 1100
"""Model fit window (the backtest's choice, ADR 0005)."""


class StakingMode(StrEnum):
    """How stake units are chosen."""

    FLAT = "flat"
    KELLY = "kelly"


@dataclass(frozen=True, slots=True, kw_only=True)
class PipelineConfig:
    """Configuration of the analysis, refresh and notification jobs."""

    bet_bookmaker: str = BOOKMAKER_1XBET
    """Whose price the user bets at (decision #10); fair probabilities come from Pinnacle."""
    model_weight: float = DEFAULT_MODEL_WEIGHT
    xi: float = DEFAULT_XI_PER_DAY
    history_days: int = HISTORY_DAYS
    min_team_matches: int = MIN_TEAM_MATCHES
    staking: StakingMode = StakingMode.FLAT
    flat_unit: Decimal = Decimal(1)
    refresh_days_ahead: int = 3
    """Days of fixtures the daily refresh loads (today included)."""
    odds_days_ahead: int = 2
    """Days of odds the daily refresh loads (today included)."""
    xg_per_refresh: int = 10
    """Most newly finished fixtures per refresh for which expected goals are fetched."""
    fixtures_ttl: timedelta = timedelta(hours=12)
    odds_ttl: timedelta = timedelta(hours=3)
    match_duration: timedelta = timedelta(hours=2)
    """After this the match is considered over (pending results, settle candidates)."""
    daily_catch_up: timedelta = timedelta(hours=12)
    """A digest missed by more than this (the process was down) is skipped, not sent late."""
    before_kickoff_lead: timedelta = timedelta(hours=24)
    """How long before kick-off the first scheduled recalculation runs."""
    odds_move_threshold: float = 0.10
    review_timeout: float = DEFAULT_REVIEW_TIMEOUT
    history_sources: tuple[str, ...] = ("api-football", "football-data")
    """Which stored copy of the same match the model fit prefers."""

    def __post_init__(self) -> None:
        """Reject values that would make a job meaningless."""
        if not 0.0 <= self.model_weight <= 1.0:
            msg = f"model_weight must be within [0, 1], got {self.model_weight}"
            raise ConfigurationError(msg)
        if self.refresh_days_ahead < 1 or self.odds_days_ahead < 1:
            msg = "refresh_days_ahead and odds_days_ahead must be at least 1"
            raise ConfigurationError(msg)
