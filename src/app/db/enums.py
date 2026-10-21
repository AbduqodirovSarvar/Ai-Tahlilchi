"""Storage vocabularies for signal, user and feedback tables.

Sports, markets, selections, fixture statuses and odds kinds come from ``app.domain``;
the enums here describe concepts that only exist once the system starts producing signals.
Values are persisted, so they are stable identifiers: add members, never rename them.
"""

from enum import StrEnum


class SignalSource(StrEnum):
    """Which flow produced a signal."""

    DAILY = "daily"
    CUSTOM = "custom"
    WATCHLIST = "watchlist"


class SignalStatus(StrEnum):
    """Visibility/validity of a signal."""

    ACTIVE = "active"
    CANCELLED = "cancelled"
    HIDDEN = "hidden"


class Confidence(StrEnum):
    """Trust level of a calculation (separate from its probability; plan §6.6)."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class LlmVerdict(StrEnum):
    """Gemini's context review of a candidate (plan §4, step 4)."""

    CONFIRM = "confirm"
    CAUTION = "caution"
    REJECT = "reject"


class SignalVersionReason(StrEnum):
    """Why a new version of a signal's numbers was stored."""

    INITIAL = "initial"
    RECALC = "recalc"
    PREMATCH = "prematch"


class Outcome(StrEnum):
    """Settlement of a signal or bet. Half results come from quarter Asian handicap lines."""

    WIN = "win"
    LOSS = "loss"
    VOID = "void"
    HALF_WIN = "half_win"
    HALF_LOSS = "half_loss"
    CASHOUT = "cashout"


class ResultSource(StrEnum):
    """Who settled a signal."""

    API = "api"
    MANUAL = "manual"


class PostmortemCategory(StrEnum):
    """Root cause of a lost signal (plan §6.13, layer 4)."""

    BAD_LUCK = "bad_luck"
    MISSING_INFO = "missing_info"
    MODEL_ERROR = "model_error"


class LessonStatus(StrEnum):
    """Lifecycle of a lesson: active after repeated confirmation, expired after 90 days."""

    PENDING = "pending"
    ACTIVE = "active"
    EXPIRED = "expired"


class WatchKind(StrEnum):
    """What a watchlist entry follows."""

    TEAM = "team"
    LEAGUE = "league"


class RiskProfile(StrEnum):
    """Preset of probability/EV/odds thresholds (plan §6.5); ``custom`` = user-edited."""

    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"
    CUSTOM = "custom"


class DateMode(StrEnum):
    """How a saved query picks its date range."""

    TODAY = "today"
    TOMORROW = "tomorrow"
    WEEKEND = "weekend"
    NEXT_DAYS = "next_days"
    RANGE = "range"


class QueryScope(StrEnum):
    """Which fixtures a saved query covers."""

    ALL = "all"
    LEAGUES = "leagues"
    TEAMS = "teams"
    FIXTURES = "fixtures"


class SortOrder(StrEnum):
    """Ordering of analysis results."""

    EV = "ev"
    PROBABILITY = "probability"
    KICKOFF = "kickoff"


class RecalcKind(StrEnum):
    """Automatic recalculation moments of a signal."""

    BEFORE_24H = "before_24h"
    PREMATCH = "prematch"
