"""Tunable numbers of the pipeline (nothing here is read from the environment)."""

from dataclasses import dataclass
from datetime import time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final
from zoneinfo import ZoneInfo

from app.domain import BOOKMAKER_1XBET, BOOKMAKER_PINNACLE
from app.errors import ConfigurationError
from app.models import DEFAULT_MODEL_WEIGHT, DEFAULT_XI_PER_DAY
from app.models.confidence import MIN_TEAM_MATCHES
from app.pipeline.review import DEFAULT_REVIEW_TIMEOUT, DEFAULT_REVIEW_TOTAL_TIMEOUT

HISTORY_DAYS: Final = 1100
"""Model fit window (the backtest's choice, ADR 0005)."""
DEFAULT_DISPLAY_ZONE: Final = ZoneInfo("Asia/Tashkent")
DEFAULT_REFRESH_AT: Final = time(8, 30)
DEFAULT_MANUAL_REFRESH_MIN_INTERVAL: Final = timedelta(minutes=30)
DEFAULT_MAX_QUOTE_AGE: Final = timedelta(hours=30)
"""One daily refresh plus slack: analysis reads the database only (ADR 0021)."""


class StakingMode(StrEnum):
    """How stake units are chosen."""

    FLAT = "flat"
    KELLY = "kelly"


@dataclass(frozen=True, slots=True, kw_only=True)
class PipelineConfig:
    """Configuration of the analysis, refresh and notification jobs."""

    @property
    def ingest_bookmakers(self) -> frozenset[str]:
        """Bookmakers whose odds are fetched and stored (any other quote only grows the database).

        The sharp reference, the user's own bookmaker and the configured extras.
        """
        return frozenset({BOOKMAKER_PINNACLE, self.bet_bookmaker, *self.extra_bookmakers})

    bet_bookmaker: str = BOOKMAKER_1XBET
    """Whose price the user bets at (decision #10); fair probabilities come from Pinnacle."""
    extra_bookmakers: frozenset[str] = frozenset()
    """Bookmakers stored besides Pinnacle and ``bet_bookmaker`` (default none: see ADR 0018)."""
    model_weight: float = DEFAULT_MODEL_WEIGHT
    xi: float = DEFAULT_XI_PER_DAY
    history_days: int = HISTORY_DAYS
    min_team_matches: int = MIN_TEAM_MATCHES
    staking: StakingMode = StakingMode.FLAT
    flat_unit: Decimal = Decimal(1)
    display_zone: ZoneInfo = DEFAULT_DISPLAY_ZONE
    """The zone whose calendar days the refresh covers (a local day spans two UTC days)."""
    refresh_at: time = DEFAULT_REFRESH_AT
    """Local (``display_zone``) wall-clock time of the daily refresh job."""
    manual_refresh_min_interval: timedelta = DEFAULT_MANUAL_REFRESH_MIN_INTERVAL
    """Shortest gap between two administrator-triggered refreshes (protects the daily quota)."""
    refresh_days_ahead: int = 3
    """Local days of fixtures the daily refresh loads (today included)."""
    odds_days_ahead: int = 2
    """Local days of odds the daily refresh loads (today included)."""
    xg_per_refresh: int = 10
    """Most newly finished fixtures per refresh for which expected goals are fetched."""
    max_quote_age: timedelta = DEFAULT_MAX_QUOTE_AGE
    """Oldest bookmaker quote a decision may use; older = no decision.

    Also the age at which the database counts as not updated (``DataStaleError``)."""
    max_quote_skew: timedelta = timedelta(hours=3)
    """Largest gap between the bet quote and the anchor quote it is compared with."""
    match_duration: timedelta = timedelta(hours=2)
    """After this the match is considered over (pending results, settle candidates)."""
    daily_catch_up: timedelta = timedelta(hours=12)
    """A digest missed by more than this (the process was down) is skipped, not sent late."""
    before_kickoff_lead: timedelta = timedelta(hours=24)
    """How long before kick-off the first scheduled recalculation runs."""
    postmortems_per_run: int = 10
    """Most LLM post-mortem calls per daily run (the free tier is shared with the reviews)."""
    postmortem_lookback: timedelta = timedelta(days=30)
    """A loss older than this since settlement is not explained any more (lessons age out)."""
    odds_move_threshold: float = 0.10
    review_timeout: float = DEFAULT_REVIEW_TIMEOUT
    """Seconds one LLM review batch may take (T6-F-6)."""
    review_total_timeout: float = DEFAULT_REVIEW_TOTAL_TIMEOUT
    """Seconds all the review batches of one analysis may take together."""
    history_sources: tuple[str, ...] = ("api-football", "football-data")
    """Which stored copy of the same match the model fit prefers."""
    call_log_retention: timedelta = timedelta(days=30)
    """How long the log of provider calls is kept (``CALL_LOG_RETENTION_DAYS``)."""

    def __post_init__(self) -> None:
        """Reject values that would make a job meaningless."""
        if not 0.0 <= self.model_weight <= 1.0:
            msg = f"model_weight must be within [0, 1], got {self.model_weight}"
            raise ConfigurationError(msg)
        if self.max_quote_age <= timedelta(0) or self.max_quote_skew <= timedelta(0):
            msg = "max_quote_age and max_quote_skew must be positive"
            raise ConfigurationError(msg)
        if self.postmortems_per_run < 1 or self.postmortem_lookback <= timedelta(0):
            msg = "postmortems_per_run must be at least 1 and postmortem_lookback positive"
            raise ConfigurationError(msg)
        if self.manual_refresh_min_interval < timedelta(0):
            msg = "manual_refresh_min_interval must not be negative"
            raise ConfigurationError(msg)
        if self.refresh_days_ahead < 1 or self.odds_days_ahead < 1:
            msg = "refresh_days_ahead and odds_days_ahead must be at least 1"
            raise ConfigurationError(msg)
        if self.call_log_retention <= timedelta(0):
            msg = "call_log_retention must be positive"
            raise ConfigurationError(msg)
