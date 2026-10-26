"""Domain models shared by every layer (immutable value objects, no I/O).

This package is a frozen contract (``docs/contracts.md`` §1): names exported here may only
be added to, never changed or removed, without the orchestrator's approval.
"""

from app.domain.batch import HistoryBatch
from app.domain.call_log import (
    MAX_LOGGED_TEXT,
    CallOutcome,
    CallRecord,
    CallRecorder,
    Provider,
    clip_text,
)
from app.domain.entities import (
    MAX_SEASON,
    MIN_SEASON,
    ExternalRef,
    Fixture,
    FixtureResult,
    FixtureStatus,
    League,
    ResultStatus,
    Team,
    normalize_name,
)
from app.domain.errors import DomainValidationError
from app.domain.markets import (
    LINE_MARKETS,
    LINE_STEP,
    SELECTIONS_BY_MARKET,
    Market,
    MarketType,
    Selection,
    normalize_line,
)
from app.domain.odds import (
    BOOKMAKER_1XBET,
    BOOKMAKER_BET365,
    BOOKMAKER_MARKET_AVERAGE,
    BOOKMAKER_MARKET_MAXIMUM,
    BOOKMAKER_PINNACLE,
    MIN_DECIMAL_ODDS_EXCLUSIVE,
    OddsKind,
    OddsSnapshot,
)
from app.domain.sport import Sport

__all__ = [
    "BOOKMAKER_1XBET",
    "BOOKMAKER_BET365",
    "BOOKMAKER_MARKET_AVERAGE",
    "BOOKMAKER_MARKET_MAXIMUM",
    "BOOKMAKER_PINNACLE",
    "LINE_MARKETS",
    "LINE_STEP",
    "MAX_LOGGED_TEXT",
    "MAX_SEASON",
    "MIN_DECIMAL_ODDS_EXCLUSIVE",
    "MIN_SEASON",
    "SELECTIONS_BY_MARKET",
    "CallOutcome",
    "CallRecord",
    "CallRecorder",
    "DomainValidationError",
    "ExternalRef",
    "Fixture",
    "FixtureResult",
    "FixtureStatus",
    "HistoryBatch",
    "League",
    "Market",
    "MarketType",
    "OddsKind",
    "OddsSnapshot",
    "Provider",
    "ResultStatus",
    "Selection",
    "Sport",
    "Team",
    "clip_text",
    "normalize_line",
    "normalize_name",
]
