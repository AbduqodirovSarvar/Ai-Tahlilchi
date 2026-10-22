"""Service contract between the Telegram bot and the rest of the system (contract #4).

The bot depends only on this module (plus ``app.domain`` value types and
``app.services.errors``). Everything here is an immutable DTO, an enum, a named constant or
an async ``Protocol``; implementations live elsewhere (``app.services.fake`` now, the
pipeline-backed services in F4). See ``docs/contracts.md`` §4 and ADR 0004.

Conventions:
- Instants are timezone-aware UTC ``datetime``; *calendar dates* chosen by the user
  (``DateSpec``, ``DateWindow``) are dates in the user's own time zone
  (``UserSettingsView.timezone``). The service converts them to UTC windows.
- Probabilities, odds and EV are ``float`` (EV ``0.05`` = +5%); money and units are
  ``Decimal``.
- Identifiers handed to the bot are opaque: ``SignalId``/``BetId`` are integers, everything
  else (``EntityId``) is a short string that fits into Telegram ``callback_data``.
- Invalid input raises :class:`~app.services.errors.ValidationFailedError` from the DTO
  constructor, so an invalid request object can never reach a service.
"""

import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final, Protocol, Self, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain import Market, MarketType, Selection, Sport
from app.services.errors import ValidationFailedError

type SignalId = int
type BetId = int
type TemplateId = int
type EntityId = str
"""Opaque id of a league, team, fixture or saved query; at most 40 UTF-8 bytes."""

MAX_ENTITY_ID_BYTES: Final = 40
"""Leaves room for an action prefix in Telegram's 64-byte ``callback_data``."""

# --------------------------------------------------------------------------- limits
MIN_PROBABILITY_LOWER: Final = 0.30
MIN_PROBABILITY_UPPER: Final = 0.95
HIGH_PROBABILITY_WARNING: Final = 0.80
"""Above this minimum probability the bot shows the §6.6 warning once."""
MIN_EV_LOWER: Final = 0.0
MIN_EV_UPPER: Final = 0.20
MAX_FUTURE_DAYS: Final = 14
"""Bookmakers publish odds 1-2 weeks ahead: a window covers at most this many days counted
from today inclusive, i.e. it ends on :func:`horizon_end` (``today + 13``) at the latest."""
MAX_WINDOW_DAYS: Final = 31
"""Longest window (inclusive day count); bounds retro analyses of past dates too."""
MAX_RETRO_LOOKBACK_DAYS: Final = 90
"""A retro window may start at most this many days before today (decision #12); together
with :data:`MAX_WINDOW_DAYS` (the retro window length) it bounds how far back the bot's
calendar and typed ranges reach."""
PRELIMINARY_AFTER: Final = timedelta(days=3)
"""Signals further away are preliminary: lineups/injuries unknown, confidence lowered."""
RECALC_COOLDOWN: Final = timedelta(minutes=10)
"""Manual recalculation of one fixture is allowed once per this period."""
MANUAL_RESERVE_SHARE: Final = 0.30
"""Share of the daily provider API limit reserved for manual requests."""
QUERY_MAX_SIGNALS_UPPER: Final = 100
SETTINGS_MAX_SIGNALS_UPPER: Final = 30
MAX_SCOPE_IDS: Final = 50
MIN_ODDS_LIMIT: Final = 1.01
MAX_ODDS_LIMIT: Final = 100.0
PREMATCH_MINUTE_CHOICES: Final = frozenset({30, 60, 90})
MAX_NOTE_LENGTH: Final = 500
MAX_TEMPLATE_NAME_LENGTH: Final = 64
MIN_EXPRESS_LEGS: Final = 2
MAX_EXPRESS_LEGS: Final = 4
"""An express combines this many signals (plan §6.11), each from a different match."""
MAX_BET_TOKEN_LENGTH: Final = 64
"""Longest ``BetDraft.client_token`` (a Telegram update or callback id fits easily)."""
MIN_SEARCH_LENGTH: Final = 2
MAX_SEARCH_RESULTS: Final = 20
DEFAULT_PAGE_SIZE: Final = 8
MAX_PAGE_SIZE: Final = 20
DEFAULT_TIMEZONE: Final = "Asia/Tashkent"
_SATURDAY: Final = 5
_SUNDAY: Final = 6
_DAYS_PER_WEEK: Final = 7


# --------------------------------------------------------------------------- validation
def _check(condition: bool, field: str, message: str) -> None:
    """Raise :class:`ValidationFailedError` for ``field`` unless ``condition`` holds."""
    if not condition:
        raise ValidationFailedError(field, message)


def _check_range(value: float, field: str, lower: float, upper: float) -> None:
    """Require a finite ``value`` within ``[lower, upper]``."""
    _check(math.isfinite(value), field, f"must be a finite number (got {value!r})")
    _check(lower <= value <= upper, field, f"must be within [{lower}, {upper}] (got {value})")


def _check_utc(value: datetime, field: str) -> None:
    """Require an aware datetime in UTC (services never hand out local or naive times)."""
    _check(value.utcoffset() == timedelta(0), field, "must be a timezone-aware UTC datetime")


def _check_text(value: str, field: str, max_length: int) -> None:
    """Require non-blank text of at most ``max_length`` characters."""
    _check(bool(value.strip()), field, "must not be empty")
    _check(len(value) <= max_length, field, f"must be at most {max_length} characters")


def _check_positive(value: Decimal | None, field: str) -> None:
    """Require ``value`` to be ``None`` or a finite, strictly positive amount."""
    if value is not None:
        _check(value.is_finite() and value > 0, field, f"must be positive (got {value})")


def is_valid_entity_id(value: str) -> bool:
    """True for a non-blank id of at most :data:`MAX_ENTITY_ID_BYTES` UTF-8 bytes."""
    return bool(value.strip()) and len(value.encode()) <= MAX_ENTITY_ID_BYTES


def _check_entity_ids(values: frozenset[str] | tuple[str, ...], field: str) -> None:
    """Every id fits the :data:`MAX_ENTITY_ID_BYTES` budget."""
    _check(
        all(is_valid_entity_id(value) for value in values),
        field,
        f"ids must be non-blank and at most {MAX_ENTITY_ID_BYTES} bytes",
    )


# --------------------------------------------------------------------------- enums
class ScopeKind(StrEnum):
    """Which fixtures an analysis covers (plan §6.2, step 4)."""

    ALL = "all"
    LEAGUES = "leagues"
    TEAMS = "teams"
    FIXTURES = "fixtures"


class SortKey(StrEnum):
    """Ordering of analysis results; EV and probability descending, kickoff ascending."""

    EV = "ev"
    PROBABILITY = "probability"
    KICKOFF = "kickoff"


class DateMode(StrEnum):
    """How a :class:`DateSpec` picks its days (plan §6.2, step 1)."""

    TODAY = "today"
    TOMORROW = "tomorrow"
    WEEKEND = "weekend"
    NEXT_DAYS = "next_days"
    RANGE = "range"


class ConfidenceLevel(StrEnum):
    """Trust in the calculation itself, independent of its probability (plan §6.6)."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Verdict(StrEnum):
    """Gemini's context review of a signal (plan §4, step 4)."""

    CONFIRM = "confirm"
    CAUTION = "caution"
    REJECT = "reject"


class SignalState(StrEnum):
    """Visibility/validity of a signal."""

    ACTIVE = "active"
    CANCELLED = "cancelled"
    HIDDEN = "hidden"


class SignalSource(StrEnum):
    """Which flow produced a signal.

    ``retro`` signals describe matches that had already kicked off when analysed: they show
    what the model would have said, are never recommendations and never enter reports.
    """

    ANALYSIS = "analysis"
    DAILY = "daily"
    CUSTOM = "custom"
    WATCHLIST = "watchlist"
    RETRO = "retro"


class ResultMark(StrEnum):
    """Settlement of a signal or bet. Half results come from quarter Asian lines."""

    WON = "win"
    LOST = "loss"
    VOID = "void"
    HALF_WON = "half_win"
    HALF_LOST = "half_loss"
    CASHOUT = "cashout"


MANUAL_RESULT_MARKS: Final = frozenset(
    {ResultMark.WON, ResultMark.LOST, ResultMark.VOID, ResultMark.CASHOUT}
)
"""Marks a user may set by hand (plan §6.13 A); half results only come from settlement."""


class RecalcVerdict(StrEnum):
    """What a recalculation means for the signal (plan §6.4)."""

    STILL_VALID = "still_valid"
    BELOW_THRESHOLD = "below_threshold"
    CANCELLED = "cancelled"
    NO_PRICE = "no_price"
    """v1.5: the user's bookmaker has no fresh price any more; the numbers are unconfirmed."""


class AnalysisStage(StrEnum):
    """Phase of a long-running analysis, reported through :data:`ProgressCallback`."""

    COLLECTING = "collecting"
    ANALYSING = "analysing"
    REVIEWING = "reviewing"
    FINISHED = "finished"


class RiskProfile(StrEnum):
    """Threshold preset (plan §6.5); ``custom`` once the user edits any threshold."""

    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"
    CUSTOM = "custom"


class WatchKind(StrEnum):
    """What a watchlist entry follows."""

    TEAM = "team"
    LEAGUE = "league"


class WatchNotification(StrEnum):
    """Optional watchlist notifications (plan §6.7)."""

    LINEUPS = "lineups"
    ODDS_MOVE = "odds_move"
    RESULT = "result"


class FormResult(StrEnum):
    """One match of a team's recent form, from that team's point of view."""

    WIN = "win"
    DRAW = "draw"
    LOSS = "loss"


class TeamSide(StrEnum):
    """Home or away side of a fixture."""

    HOME = "home"
    AWAY = "away"


class ReportGroup(StrEnum):
    """Slice of a report (plan §6.10)."""

    SPORT = "sport"
    LEAGUE = "league"
    MARKET = "market"
    RISK_PROFILE = "risk_profile"
    CONFIDENCE = "confidence"


# --------------------------------------------------------------------------- dates
@dataclass(frozen=True, slots=True)
class DateWindow:
    """Inclusive range of calendar dates in the user's time zone."""

    date_from: date
    date_to: date

    def __post_init__(self) -> None:
        """Require an ordered window of at most :data:`MAX_WINDOW_DAYS` days."""
        _check(self.date_to >= self.date_from, "date_to", "must not be before date_from")
        _check(self.days <= MAX_WINDOW_DAYS, "date_to", f"window exceeds {MAX_WINDOW_DAYS} days")

    @property
    def days(self) -> int:
        """Number of calendar days in the window (both ends included)."""
        return (self.date_to - self.date_from).days + 1

    def ensure_within_horizon(self, today: date) -> None:
        """Reject windows ending after :func:`horizon_end` (``today + 13``).

        Past windows (retro analysis) are always allowed.
        """
        _check(
            self.date_to <= horizon_end(today),
            "date_to",
            f"must be within {MAX_FUTURE_DAYS} days from today (today included)",
        )

    def ensure_within_lookback(self, today: date) -> None:
        """Reject windows starting more than :data:`MAX_RETRO_LOOKBACK_DAYS` before ``today``."""
        _check(
            self.date_from >= today - timedelta(days=MAX_RETRO_LOOKBACK_DAYS),
            "date_from",
            f"must not be more than {MAX_RETRO_LOOKBACK_DAYS} days in the past",
        )

    def ensure_allowed(self, today: date) -> None:
        """Every window rule that depends on "today": horizon and retro look-back (v1.2).

        The length limit (:data:`MAX_WINDOW_DAYS`) holds by construction. Services call this
        once per request, with their own clock, before touching data.
        """
        self.ensure_within_horizon(today)
        self.ensure_within_lookback(today)

    def is_retro(self, today: date) -> bool:
        """True when the whole window is in the past (results already known)."""
        return self.date_to < today

    def utc_bounds(self, zone: ZoneInfo) -> tuple[datetime, datetime]:
        """Half-open UTC interval ``[start, end)`` covering the window's local days.

        Local midnights are converted by ``zoneinfo``, so days around a DST change are
        23 or 25 hours long. This is the only place local dates become UTC instants.
        """
        start = datetime.combine(self.date_from, time(0), tzinfo=zone)
        end = datetime.combine(self.date_to + timedelta(days=1), time(0), tzinfo=zone)
        return start.astimezone(UTC), end.astimezone(UTC)

    def contains(self, instant: datetime, zone: ZoneInfo) -> bool:
        """True when ``instant`` falls on one of the window's local days."""
        start, end = self.utc_bounds(zone)
        return start <= instant < end


def horizon_end(today: date) -> date:
    """Last day a window may include: :data:`MAX_FUTURE_DAYS` days counting ``today``."""
    return today + timedelta(days=MAX_FUTURE_DAYS - 1)


@dataclass(frozen=True, slots=True, kw_only=True)
class DateSpec:
    """A date choice that may be relative ("tomorrow"), so saved templates stay useful.

    Build it with the class methods; :meth:`resolve` turns it into concrete dates.
    """

    mode: DateMode
    days: int | None = None
    date_from: date | None = None
    date_to: date | None = None

    def __post_init__(self) -> None:
        """Each mode carries exactly the fields it needs."""
        if self.mode is DateMode.NEXT_DAYS:
            _check(self.days is not None, "days", "required for next_days")
            _check(
                self.days is not None and 1 <= self.days <= MAX_FUTURE_DAYS,
                "days",
                f"must be within [1, {MAX_FUTURE_DAYS}]",
            )
        else:
            _check(self.days is None, "days", f"not allowed for {self.mode}")
        if self.mode is DateMode.RANGE:
            _check(
                self.date_from is not None and self.date_to is not None,
                "date_from",
                "range needs both dates",
            )
            if self.date_from is not None and self.date_to is not None:
                DateWindow(self.date_from, self.date_to)
        else:
            both_empty = self.date_from is None and self.date_to is None
            _check(both_empty, "date_from", f"dates not allowed for {self.mode}")

    @classmethod
    def today(cls) -> Self:
        """The user's current day."""
        return cls(mode=DateMode.TODAY)

    @classmethod
    def tomorrow(cls) -> Self:
        """The user's next day."""
        return cls(mode=DateMode.TOMORROW)

    @classmethod
    def weekend(cls) -> Self:
        """The coming Saturday-Sunday (the rest of it, if the weekend has started)."""
        return cls(mode=DateMode.WEEKEND)

    @classmethod
    def next_days(cls, days: int) -> Self:
        """``days`` days starting today ("3 kun" → today and the next two days)."""
        return cls(mode=DateMode.NEXT_DAYS, days=days)

    @classmethod
    def between(cls, date_from: date, date_to: date) -> Self:
        """A fixed range; may lie in the past (retro analysis)."""
        return cls(mode=DateMode.RANGE, date_from=date_from, date_to=date_to)

    def resolve(self, today: date) -> DateWindow:
        """Concrete window for ``today`` (the user's local date)."""
        match self.mode:
            case DateMode.TODAY:
                return DateWindow(today, today)
            case DateMode.TOMORROW:
                tomorrow = today + timedelta(days=1)
                return DateWindow(tomorrow, tomorrow)
            case DateMode.WEEKEND:
                return _weekend_window(today)
            case DateMode.NEXT_DAYS:
                days = self.days or 1
                return DateWindow(today, today + timedelta(days=days - 1))
            case DateMode.RANGE:
                return DateWindow(self.date_from or today, self.date_to or today)


def _weekend_window(today: date) -> DateWindow:
    """Saturday-Sunday containing or following ``today``."""
    weekday = today.weekday()
    if weekday == _SUNDAY:
        return DateWindow(today, today)
    saturday = today + timedelta(days=(_SATURDAY - weekday) % _DAYS_PER_WEEK)
    return DateWindow(saturday, saturday + timedelta(days=1))


# --------------------------------------------------------------------------- settings
@dataclass(frozen=True, slots=True, kw_only=True)
class RiskPreset:
    """Thresholds a risk profile sets."""

    min_probability: float
    min_ev: float
    odds_min: float
    odds_max: float


CONSERVATIVE_PRESET: Final = RiskPreset(
    min_probability=0.60, min_ev=0.03, odds_min=1.30, odds_max=2.00
)
BALANCED_PRESET: Final = RiskPreset(min_probability=0.45, min_ev=0.05, odds_min=1.50, odds_max=3.00)
AGGRESSIVE_PRESET: Final = RiskPreset(
    min_probability=0.30, min_ev=0.08, odds_min=2.00, odds_max=5.00
)
RISK_PRESETS: Final[Mapping[RiskProfile, RiskPreset]] = {
    RiskProfile.CONSERVATIVE: CONSERVATIVE_PRESET,
    RiskProfile.BALANCED: BALANCED_PRESET,
    RiskProfile.AGGRESSIVE: AGGRESSIVE_PRESET,
}
DEFAULT_RISK_PROFILE: Final = RiskProfile.BALANCED
DEFAULT_SPORTS: Final = frozenset({Sport.FOOTBALL})
DEFAULT_MARKETS: Final = frozenset({MarketType.MATCH_RESULT, MarketType.TOTALS})
DEFAULT_MAX_SIGNALS: Final = 10
DEFAULT_SEND_TIME: Final = time(9, 0)
DEFAULT_PREMATCH_MINUTES: Final = 60


def infer_risk_profile(
    min_probability: float, min_ev: float, odds_range: tuple[float, float]
) -> RiskProfile:
    """Profile whose preset matches the thresholds exactly, else ``custom``."""
    for profile, preset in RISK_PRESETS.items():
        if (preset.min_probability, preset.min_ev, (preset.odds_min, preset.odds_max)) == (
            min_probability,
            min_ev,
            odds_range,
        ):
            return profile
    return RiskProfile.CUSTOM


@dataclass(frozen=True, slots=True)
class QuietHours:
    """Local time span without notifications; may wrap midnight (``23:00-07:00``)."""

    start: time
    end: time

    def __post_init__(self) -> None:
        """An empty span is meaningless; use ``None`` to switch quiet hours off."""
        _check(self.start != self.end, "quiet_hours", "start and end must differ")


DEFAULT_QUIET_HOURS: Final = QuietHours(time(0, 0), time(8, 0))


@dataclass(frozen=True, slots=True, kw_only=True)
class UserSettingsView:
    """A user's settings (plan §6.5). Also the update DTO: change with ``dataclasses.replace``.

    Empty ``league_ids`` means "every tracked league"; otherwise ``scope=all`` analyses and
    unfiltered fixture lists cover only these leagues (watched entities are always added).
    ``prematch_minutes=None`` switches the pre-match update off, ``quiet_hours=None``
    switches quiet hours off.
    """

    sports: frozenset[Sport] = DEFAULT_SPORTS
    league_ids: frozenset[EntityId] = frozenset()
    markets: frozenset[MarketType] = DEFAULT_MARKETS
    max_signals: int = DEFAULT_MAX_SIGNALS
    min_probability: float = BALANCED_PRESET.min_probability
    min_ev: float = BALANCED_PRESET.min_ev
    odds_min: float = BALANCED_PRESET.odds_min
    odds_max: float = BALANCED_PRESET.odds_max
    risk_profile: RiskProfile = DEFAULT_RISK_PROFILE
    send_time: time = DEFAULT_SEND_TIME
    prematch_minutes: int | None = DEFAULT_PREMATCH_MINUTES
    quiet_hours: QuietHours | None = DEFAULT_QUIET_HOURS
    timezone: str = DEFAULT_TIMEZONE
    bank: Decimal | None = None
    unit: Decimal | None = None
    daily_loss_limit: Decimal | None = None
    weekly_loss_limit: Decimal | None = None

    def __post_init__(self) -> None:
        """Validate every user-editable value against the §6.5 ranges."""
        _check(bool(self.sports), "sports", "choose at least one sport")
        _check(bool(self.markets), "markets", "choose at least one market")
        _check(
            1 <= self.max_signals <= SETTINGS_MAX_SIGNALS_UPPER,
            "max_signals",
            f"must be within [1, {SETTINGS_MAX_SIGNALS_UPPER}]",
        )
        _check_range(
            self.min_probability, "min_probability", MIN_PROBABILITY_LOWER, MIN_PROBABILITY_UPPER
        )
        _check_range(self.min_ev, "min_ev", MIN_EV_LOWER, MIN_EV_UPPER)
        _check_range(self.odds_min, "odds_min", MIN_ODDS_LIMIT, MAX_ODDS_LIMIT)
        _check_range(self.odds_max, "odds_max", MIN_ODDS_LIMIT, MAX_ODDS_LIMIT)
        _check(self.odds_min < self.odds_max, "odds_max", "must be greater than odds_min")
        _check(
            self.prematch_minutes is None or self.prematch_minutes in PREMATCH_MINUTE_CHOICES,
            "prematch_minutes",
            f"must be one of {sorted(PREMATCH_MINUTE_CHOICES)} or off",
        )
        _check(_is_time_zone(self.timezone), "timezone", "must be an IANA time zone name")
        _check_entity_ids(self.league_ids, "league_ids")
        self._check_money()

    def _check_money(self) -> None:
        """Bank, unit and loss limits are positive; a unit never exceeds the bank."""
        for field in ("bank", "unit", "daily_loss_limit", "weekly_loss_limit"):
            _check_positive(getattr(self, field), field)
        if self.bank is not None and self.unit is not None:
            _check(self.unit <= self.bank, "unit", "must not exceed the bank")

    @property
    def zone(self) -> ZoneInfo:
        """The user's time zone, for converting UTC instants to local display time."""
        return ZoneInfo(self.timezone)

    def with_risk_profile(self, profile: RiskProfile) -> Self:
        """Copy with the preset thresholds of ``profile`` (``custom`` keeps them)."""
        preset = RISK_PRESETS.get(profile)
        if preset is None:
            return replace(self, risk_profile=profile)
        return replace(
            self,
            risk_profile=profile,
            min_probability=preset.min_probability,
            min_ev=preset.min_ev,
            odds_min=preset.odds_min,
            odds_max=preset.odds_max,
        )


def _is_time_zone(name: str) -> bool:
    """True when ``name`` is a known IANA time zone."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


# --------------------------------------------------------------------------- analysis query
@dataclass(frozen=True, slots=True, kw_only=True)
class AnalysisQuery:
    """One analysis request (plan §6.2 wizard, §6.3 custom analysis, saved templates).

    ``max_signals=None`` means "all"; with ``per_day`` the cap applies to each day.
    ``include_no_value`` (custom analysis, §6.3) disables the probability and EV filters:
    every priced selection of the chosen markets is returned and the ones below the value
    thresholds are flagged ``no_value``.
    """

    dates: DateSpec
    sports: frozenset[Sport] = DEFAULT_SPORTS
    min_probability: float = BALANCED_PRESET.min_probability
    min_ev: float = BALANCED_PRESET.min_ev
    scope: ScopeKind = ScopeKind.ALL
    scope_ids: frozenset[EntityId] = frozenset()
    markets: frozenset[MarketType] = DEFAULT_MARKETS
    max_signals: int | None = None
    per_day: bool = False
    sort: SortKey = SortKey.EV
    include_no_value: bool = False

    def __post_init__(self) -> None:
        """Validate thresholds, scope and count; the date horizon is checked on resolve."""
        _check(bool(self.sports), "sports", "choose at least one sport")
        _check(bool(self.markets), "markets", "choose at least one market")
        _check_range(
            self.min_probability, "min_probability", MIN_PROBABILITY_LOWER, MIN_PROBABILITY_UPPER
        )
        _check_range(self.min_ev, "min_ev", MIN_EV_LOWER, MIN_EV_UPPER)
        if self.scope is ScopeKind.ALL:
            _check(not self.scope_ids, "scope_ids", "must be empty for scope 'all'")
        else:
            _check(bool(self.scope_ids), "scope_ids", f"choose at least one for {self.scope}")
        _check(len(self.scope_ids) <= MAX_SCOPE_IDS, "scope_ids", f"at most {MAX_SCOPE_IDS}")
        _check_entity_ids(self.scope_ids, "scope_ids")
        _check(
            self.max_signals is None or 1 <= self.max_signals <= QUERY_MAX_SIGNALS_UPPER,
            "max_signals",
            f"must be within [1, {QUERY_MAX_SIGNALS_UPPER}] or all",
        )
        _check(not self.per_day or self.max_signals is not None, "per_day", "needs max_signals")

    @classmethod
    def from_settings(cls, settings: UserSettingsView, dates: DateSpec) -> Self:
        """Query with the user's defaults; parameters not given in ``/tahlil`` come from here."""
        return cls(
            dates=dates,
            sports=settings.sports,
            min_probability=settings.min_probability,
            min_ev=settings.min_ev,
            markets=settings.markets,
            max_signals=settings.max_signals,
        )


# --------------------------------------------------------------------------- analysis results
@dataclass(frozen=True, slots=True, kw_only=True)
class CostEstimate:
    """What an analysis would cost, shown before the user presses "Boshlash"."""

    window: DateWindow
    fixtures_found: int
    api_requests: int
    budget_remaining: int

    @property
    def within_budget(self) -> bool:
        """True when the manual API reserve covers the request."""
        return self.api_requests <= self.budget_remaining


@dataclass(frozen=True, slots=True, kw_only=True)
class AnalysisProgress:
    """Progress of a long-running job: ``done`` of ``total`` fixtures in ``stage``."""

    stage: AnalysisStage
    done: int
    total: int


type ProgressCallback = Callable[[AnalysisProgress], Awaitable[None]]
"""Awaited by services between steps.

A failing callback never aborts the job: services catch and log any ``Exception`` it
raises and carry on (``asyncio.CancelledError`` still propagates). Services may call it
once per fixture, so the bot throttles its message edits (Telegram flood limits).
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalView:
    """Everything a signal card shows (plan §6.2), plus the ids its buttons need.

    ``no_value``: below the user's value thresholds, shown only because of a custom
    analysis or the watchlist. ``data_as_of``: set when numbers come from a cache because
    the API reserve is spent ("09:00 holatiga"). ``experimental``: sport not yet
    backtested ("🧪 Test rejimi").

    ``recommended_at``: when the selection first passed the value thresholds of a query
    (``None`` = never recommended). Once set it never changes and ``no_value`` stays
    ``False``, whatever later queries say. Only recommended, non-retro signals count in
    reports, calibration, day summaries and exports; hiding a signal does not change that.

    v1.5 (decision #17): ``odds`` are the *current* odds (recalculations move them);
    ``recommended_odds`` are the odds first recommended, never changed, and the ones the
    signal's profit and CLV are judged on (``None`` while never recommended; then ``odds``
    stand in). ``zero_ev_odds`` is the price at which the pick has zero EV for the model
    (``None`` = unknown); it differs from ``1 / probability`` on quarter-line Asian picks.
    ``data_as_of`` is the time of the bookmaker price the numbers rest on.

    v1.7: ``home_team_id``, ``away_team_id`` and ``league_id`` are the catalog entity ids (the
    same ids ``CatalogService`` hands out), so a card's follow buttons need no name search;
    ``None`` when the source does not know them.
    """

    signal_id: SignalId
    fixture_id: EntityId
    sport: Sport
    league: str
    home_team: str
    away_team: str
    kickoff: datetime
    market: Market
    selection: Selection
    odds: float
    bookmaker: str
    probability: float
    ev: float
    confidence: ConfidenceLevel
    stake_units: Decimal
    llm_verdict: Verdict | None = None
    llm_note: str | None = None
    risk_flags: tuple[str, ...] = ()
    preliminary: bool = False
    no_value: bool = False
    watchlist: bool = False
    experimental: bool = False
    state: SignalState = SignalState.ACTIVE
    result: ResultMark | None = None
    version: int = 1
    data_as_of: datetime | None = None
    source: SignalSource = SignalSource.ANALYSIS
    recommended_at: datetime | None = None
    recommended_odds: float | None = None
    zero_ev_odds: float | None = None
    home_team_id: EntityId | None = None
    away_team_id: EntityId | None = None
    league_id: EntityId | None = None

    def __post_init__(self) -> None:
        """Guard the invariants the card formatter relies on."""
        _check_utc(self.kickoff, "kickoff")
        _check(is_valid_entity_id(self.fixture_id), "fixture_id", "invalid entity id")
        for name in ("home_team_id", "away_team_id", "league_id"):
            value = getattr(self, name)
            _check(value is None or is_valid_entity_id(value), name, "invalid entity id")
        if self.recommended_at is not None:
            _check_utc(self.recommended_at, "recommended_at")
            _check(not self.no_value, "no_value", "a recommended signal has value")
        _check(
            self.source is not SignalSource.RETRO or self.recommended_at is None,
            "recommended_at",
            "retro signals are never recommendations",
        )
        self.market.require_selection(self.selection)
        _check(self.odds > 1.0, "odds", "decimal odds must be greater than 1")
        _check(
            self.recommended_odds is None or self.recommended_odds > 1.0,
            "recommended_odds",
            "decimal odds must be greater than 1",
        )
        _check(
            self.zero_ev_odds is None or self.zero_ev_odds > 1.0,
            "zero_ev_odds",
            "decimal odds must be greater than 1",
        )
        _check(0.0 < self.probability <= 1.0, "probability", "must be within (0, 1]")
        _check(self.stake_units >= 0, "stake_units", "must not be negative")
        _check(self.version >= 1, "version", "must be at least 1")

    @property
    def fixture_label(self) -> str:
        """``Home — Away``."""
        return f"{self.home_team} — {self.away_team}"

    @property
    def fair_odds(self) -> float:
        """Odds at which this selection would have zero EV for the model.

        The stored zero-EV price when known (quarter-line Asian picks count half wins and
        half losses, so it is not ``1 / probability``), else ``1 / probability``.
        """
        return 1.0 / self.probability if self.zero_ev_odds is None else self.zero_ev_odds

    @property
    def profit_odds(self) -> float:
        """The odds the signal's profit and CLV are judged on: first recommended, else current."""
        return self.odds if self.recommended_odds is None else self.recommended_odds

    @property
    def negative_ev(self) -> bool:
        """True for "❗ Qiymatsiz" signals (plan §6.6)."""
        return self.ev < 0

    @property
    def retro(self) -> bool:
        """True for a look-back at a match that had kicked off when analysed."""
        return self.source is SignalSource.RETRO

    @property
    def recommended(self) -> bool:
        """True once the selection has been recommended (it then counts in reports)."""
        return self.recommended_at is not None


class LimitPeriod(StrEnum):
    """Which responsible-gaming limit was reached (v1.7)."""

    DAY = "day"
    WEEK = "week"


@dataclass(frozen=True, slots=True, kw_only=True)
class LossLimitNotice:
    """A loss limit that has been reached (plan 6.9; v1.7).

    ``limit`` and ``loss`` are money (the bank currency): ``loss`` is the user's net loss on
    bets settled in the local day (``DAY``) or ISO week so far (``WEEK``), ``loss >= limit``.
    A message carrying it says "Zarar limitiga yetildi"; an analysis result carrying it is a
    banner on a manual analysis, which still works.
    """

    period: LimitPeriod
    limit: Decimal
    loss: Decimal

    def __post_init__(self) -> None:
        """A reached limit is positive and the loss has caught up with it."""
        _check_positive(self.limit, "limit")
        _check(self.loss >= self.limit, "loss", "must have reached the limit")


@dataclass(frozen=True, slots=True, kw_only=True)
class AnalysisResult:
    """Outcome of :meth:`AnalysisService.analyze`.

    ``matched`` counts signals that met the conditions before the ``max_signals`` cap, so
    the bot can say "3 matched (you asked for 10)". Watchlist signals (plan §6.7, "always
    included") are added on top: they are neither cut by ``max_signals`` nor counted in
    ``matched``. Fixtures that failed are counted in ``failed_fixtures`` and skipped; the
    rest are still analysed. ``signals`` is ordered by the query's sort key.
    """

    query_id: EntityId
    window: DateWindow
    signals: tuple[SignalView, ...]
    fixtures_analyzed: int
    matched: int
    requested: int | None
    api_requests_used: int
    failed_fixtures: int = 0
    retro: bool = False
    skipped_no_price: int = 0
    skipped_stale_price: int = 0
    data_stale: bool = False
    loss_limit: LossLimitNotice | None = None

    @property
    def shortfall(self) -> bool:
        """True when fewer signals matched than the user asked for."""
        return self.requested is not None and self.matched < self.requested


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalNumbers:
    """The numbers of one signal version, compared by :class:`RecalcDiff`."""

    odds: float
    probability: float
    ev: float
    stake_units: Decimal
    confidence: ConfidenceLevel


@dataclass(frozen=True, slots=True, kw_only=True)
class RecalcDiff:
    """Before/after of a recalculation (plan §6.4); ``signal`` is the updated card."""

    signal: SignalView
    before: SignalNumbers
    after: SignalNumbers
    verdict: RecalcVerdict
    recalculated_at: datetime

    def __post_init__(self) -> None:
        """Recalculation times are UTC like every other instant."""
        _check_utc(self.recalculated_at, "recalculated_at")

    @property
    def changed(self) -> bool:
        """True when any number moved."""
        return self.before != self.after


@dataclass(frozen=True, slots=True, kw_only=True)
class RecalcBatch:
    """Result of recalculating every signal of an earlier analysis."""

    query_id: EntityId
    diffs: tuple[RecalcDiff, ...]
    skipped_cooldown: tuple[SignalId, ...] = ()
    skipped_started: tuple[SignalId, ...] = ()
    failed: tuple[SignalId, ...] = ()


# --------------------------------------------------------------------------- fixture detail
@dataclass(frozen=True, slots=True, kw_only=True)
class TeamStats:
    """Averages of a team in its venue role (home team at home, away team away)."""

    matches: int
    goals_for_avg: float
    goals_against_avg: float
    xg_for_avg: float | None = None
    xg_against_avg: float | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class HeadToHead:
    """One earlier meeting of the two teams."""

    kickoff: datetime
    home_team: str
    away_team: str
    home_score: int
    away_score: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Absence:
    """An injured or suspended player."""

    side: TeamSide
    player: str
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ScoreProbability:
    """Probability of an exact score."""

    home_goals: int
    away_goals: int
    probability: float


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketQuote:
    """One row of the all-markets table; ``odds``/``ev`` are ``None`` without a price."""

    market: Market
    selection: Selection
    probability: float
    odds: float | None = None
    ev: float | None = None

    @property
    def fair_odds(self) -> float:
        """Odds at which this selection would have zero EV for the model."""
        return 1.0 / self.probability


@dataclass(frozen=True, slots=True, kw_only=True)
class OddsMove:
    """Odds movement of a selection between the morning snapshot and now."""

    market: Market
    selection: Selection
    opening: float
    current: float

    @property
    def change(self) -> float:
        """Relative change; negative when the price shortened."""
        return self.current / self.opening - 1.0


@dataclass(frozen=True, slots=True, kw_only=True)
class FixtureDetail:
    """The detail card (plan §6.8). Forms are oldest first, at most five matches."""

    fixture_id: EntityId
    sport: Sport
    league: str
    home_team: str
    away_team: str
    kickoff: datetime
    home_form: tuple[FormResult, ...]
    away_form: tuple[FormResult, ...]
    home_stats: TeamStats
    away_stats: TeamStats
    head_to_head: tuple[HeadToHead, ...]
    absences: tuple[Absence, ...]
    top_scores: tuple[ScoreProbability, ...]
    markets: tuple[MarketQuote, ...]
    odds_moves: tuple[OddsMove, ...]
    lineups_confirmed: bool = False
    home_score: int | None = None
    away_score: int | None = None
    data_as_of: datetime | None = None


# --------------------------------------------------------------------------- catalog
@dataclass(frozen=True, slots=True, kw_only=True)
class SportOption:
    """A sport for the sport picker and the markets the bot may offer for it.

    ``available=False``: not supported yet (show disabled or hide). ``experimental``: no
    backtest yet, signals carry the "🧪 Test rejimi" flag (plan §5).
    """

    sport: Sport
    available: bool
    experimental: bool
    supported_markets: frozenset[MarketType]


@dataclass(frozen=True, slots=True, kw_only=True)
class LeagueOption:
    """A league for the checkbox picker."""

    league_id: EntityId
    sport: Sport
    name: str
    country: str | None = None

    def __post_init__(self) -> None:
        """Ids end up in ``callback_data``."""
        _check(is_valid_entity_id(self.league_id), "league_id", "invalid entity id")


@dataclass(frozen=True, slots=True, kw_only=True)
class TeamOption:
    """A team found by name search."""

    team_id: EntityId
    sport: Sport
    name: str
    league: str

    def __post_init__(self) -> None:
        """Ids end up in ``callback_data``."""
        _check(is_valid_entity_id(self.team_id), "team_id", "invalid entity id")


@dataclass(frozen=True, slots=True, kw_only=True)
class FixtureOption:
    """A fixture for the checkbox picker and team-search results."""

    fixture_id: EntityId
    sport: Sport
    league: str
    home_team: str
    away_team: str
    kickoff: datetime

    def __post_init__(self) -> None:
        """Ids end up in ``callback_data``; kickoffs are UTC like every instant."""
        _check(is_valid_entity_id(self.fixture_id), "fixture_id", "invalid entity id")
        _check_utc(self.kickoff, "kickoff")

    @property
    def label(self) -> str:
        """``Home — Away``."""
        return f"{self.home_team} — {self.away_team}"


@dataclass(frozen=True, slots=True, kw_only=True)
class FixtureListRequest:
    """One page of fixtures, ordered by kickoff (plan §6.2 step 4, §6.3).

    Without league and team filters the user's ``settings.league_ids`` apply (if set).
    """

    dates: DateSpec
    sports: frozenset[Sport] = DEFAULT_SPORTS
    league_ids: frozenset[EntityId] = frozenset()
    team_ids: frozenset[EntityId] = frozenset()
    page: int = 0
    page_size: int = DEFAULT_PAGE_SIZE

    def __post_init__(self) -> None:
        """Pages are zero-based; page size is bounded by Telegram keyboard limits."""
        _check(bool(self.sports), "sports", "choose at least one sport")
        _check(self.page >= 0, "page", "must not be negative")
        _check(1 <= self.page_size <= MAX_PAGE_SIZE, "page_size", f"must be 1..{MAX_PAGE_SIZE}")
        _check_entity_ids(self.league_ids, "league_ids")
        _check_entity_ids(self.team_ids, "team_ids")


@dataclass(frozen=True, slots=True, kw_only=True)
class FixturePage:
    """A page of fixtures; ``page_count`` is 0 when nothing matched."""

    items: tuple[FixtureOption, ...]
    page: int
    page_count: int
    total: int


# --------------------------------------------------------------------------- templates
@dataclass(frozen=True, slots=True, kw_only=True)
class TemplateSchedule:
    """Run a template automatically on these ISO weekdays (1=Mon..7=Sun) at local ``at``."""

    weekdays: frozenset[int]
    at: time

    def __post_init__(self) -> None:
        """At least one valid weekday."""
        _check(bool(self.weekdays), "weekdays", "choose at least one day")
        _check(all(1 <= day <= _DAYS_PER_WEEK for day in self.weekdays), "weekdays", "must be 1..7")


@dataclass(frozen=True, slots=True, kw_only=True)
class TemplateDraft:
    """A query saved under a name (plan §6.2 "💾 Shablonlar"); ``dates`` stay relative."""

    name: str
    query: AnalysisQuery
    schedule: TemplateSchedule | None = None

    def __post_init__(self) -> None:
        """Names are shown on buttons, so they are short and non-blank."""
        _check_text(self.name, "name", MAX_TEMPLATE_NAME_LENGTH)


@dataclass(frozen=True, slots=True, kw_only=True)
class TemplateView:
    """A saved template."""

    template_id: TemplateId
    name: str
    query: AnalysisQuery
    schedule: TemplateSchedule | None = None


# --------------------------------------------------------------------------- watchlist
@dataclass(frozen=True, slots=True)
class WatchTarget:
    """What to follow: a team or a league by its catalog id."""

    kind: WatchKind
    entity_id: EntityId

    def __post_init__(self) -> None:
        """Ids come from the catalog; blank or over-long ids are a bot bug."""
        _check(bool(self.entity_id.strip()), "entity_id", "must not be empty")
        _check(is_valid_entity_id(self.entity_id), "entity_id", "invalid entity id")


@dataclass(frozen=True, slots=True, kw_only=True)
class WatchItem:
    """A watchlist entry with the notifications switched on for it."""

    target: WatchTarget
    name: str
    sport: Sport
    notifications: frozenset[WatchNotification] = frozenset()


# --------------------------------------------------------------------------- bets and bank
@dataclass(frozen=True, slots=True, kw_only=True)
class BetDraft:
    """A bet placed on a signal ("✅ Stavka qildim"); ``None`` odds/stake take the signal's.

    ``client_token`` (v1.2) makes placing idempotent: the same user sending the same token
    again (a double tap, a retried callback) gets the bet that was recorded the first time
    instead of a duplicate. Use something unique per user action, e.g. the callback query id.
    """

    signal_id: SignalId
    odds_taken: float | None = None
    stake: Decimal | None = None
    client_token: str | None = None

    def __post_init__(self) -> None:
        """Odds above 1, positive stake, a short non-blank token."""
        if self.odds_taken is not None:
            _check_range(self.odds_taken, "odds_taken", MIN_ODDS_LIMIT, MAX_ODDS_LIMIT)
        _check_positive(self.stake, "stake")
        if self.client_token is not None:
            _check_text(self.client_token, "client_token", MAX_BET_TOKEN_LENGTH)


@dataclass(frozen=True, slots=True, kw_only=True)
class BetView:
    """A bet the user placed; ``outcome``/``profit`` are ``None`` until settled."""

    bet_id: BetId
    signal_id: SignalId | None
    fixture_label: str
    market: Market
    selection: Selection
    odds_taken: float
    stake: Decimal
    placed_at: datetime
    outcome: ResultMark | None = None
    profit: Decimal | None = None


@dataclass(frozen=True, slots=True)
class BankPoint:
    """Bank balance at the end of a day, for the ``/bank`` chart."""

    day: date
    balance: Decimal


@dataclass(frozen=True, slots=True, kw_only=True)
class BankView:
    """``/bank`` (plan §6.9). ``balance`` is ``None`` until the user sets a bank.

    Bank semantics (v1.2): ``UserSettingsView.bank`` is the **starting bank**. ``balance`` =
    starting bank + the profit of every *settled* bet of the user (open stakes do not reduce
    it). Editing the bank therefore re-bases the whole history: ``history`` is recomputed
    from the new starting bank, no balance snapshots are kept. Days are the local dates on
    which the bets were settled; ``loss_today``/``loss_this_week`` are net losses over those days.
    """

    balance: Decimal | None
    unit: Decimal | None
    month_profit: Decimal
    open_bets: int
    history: tuple[BankPoint, ...]
    loss_today: Decimal
    loss_this_week: Decimal
    loss_limit_reached: bool


# --------------------------------------------------------------------------- express
@dataclass(frozen=True, slots=True, kw_only=True)
class ExpressLeg:
    """One pick of an express (v1.6): the signal's current odds and model probability."""

    signal_id: SignalId
    fixture_label: str
    market: Market
    selection: Selection
    odds: float
    probability: float


@dataclass(frozen=True, slots=True, kw_only=True)
class ExpressView:
    """An express built from 2-4 signals of different matches (plan §6.11, v1.6).

    The legs are independent (different matches), so ``combined_probability`` is the product
    of the leg probabilities and ``combined_odds`` the product of the odds; ``ev`` =
    ``combined_probability * combined_odds - 1``. Several 70% picks make a much weaker bet
    (three of them: about 34%), which the bot spells out.
    """

    legs: tuple[ExpressLeg, ...]
    combined_odds: float
    combined_probability: float
    ev: float

    @property
    def fair_odds(self) -> float:
        """Combined odds at which the express would have zero EV."""
        return 1.0 / self.combined_probability


# --------------------------------------------------------------------------- results
@dataclass(frozen=True, slots=True, kw_only=True)
class ResultMarkRequest:
    """Manual settlement (plan §6.13 A). Cash-outs need the realised profit in units."""

    signal_id: SignalId
    mark: ResultMark
    cashout_profit_units: Decimal | None = None

    def __post_init__(self) -> None:
        """Only manual marks; the cash-out amount is given exactly for cash-outs."""
        _check(self.mark in MANUAL_RESULT_MARKS, "mark", "cannot be set by hand")
        is_cashout = self.mark is ResultMark.CASHOUT
        has_amount = self.cashout_profit_units is not None
        _check(is_cashout == has_amount, "cashout_profit_units", "required only for cash-outs")
        if self.cashout_profit_units is not None:
            _check(self.cashout_profit_units.is_finite(), "cashout_profit_units", "not finite")


@dataclass(frozen=True, slots=True, kw_only=True)
class DaySummary:
    """One day's outcome: "Kecha: 6 ta signal · 4 ✅ · 2 ❌ · +2.3 unit" (plan §6.13 A)."""

    day: date
    signals: int
    won: int
    lost: int
    void: int
    pending: int
    profit_units: Decimal
    clv: float | None


# --------------------------------------------------------------------------- reports
@dataclass(frozen=True, slots=True, kw_only=True)
class ReportRequest:
    """Report period (local dates, inclusive) and slice.

    ``date_from=None`` = all time; ``date_to=None`` = the user's today as the service sees
    it. ``recent_days=N`` = the last N days ending today (resolved by the service, so the
    bot never needs "today"); it excludes explicit dates.
    """

    date_to: date | None = None
    date_from: date | None = None
    group_by: ReportGroup | None = None
    recent_days: int | None = None

    def __post_init__(self) -> None:
        """An ordered period; a relative period carries no explicit dates."""
        if self.date_from is not None and self.date_to is not None:
            _check(self.date_to >= self.date_from, "date_to", "must not be before date_from")
        if self.recent_days is not None:
            _check(self.recent_days >= 1, "recent_days", "must be at least 1")
            no_dates = self.date_from is None and self.date_to is None
            _check(no_dates, "recent_days", "cannot be combined with explicit dates")

    @classmethod
    def last_days(cls, days: int, today: date, group_by: ReportGroup | None = None) -> Self:
        """``days`` days ending ``today`` ("7 kun", "30 kun")."""
        _check(days >= 1, "days", "must be at least 1")
        return cls(date_from=today - timedelta(days=days - 1), date_to=today, group_by=group_by)

    @classmethod
    def recent(cls, days: int, group_by: ReportGroup | None = None) -> Self:
        """The last ``days`` days ending on the service's today."""
        return cls(recent_days=days, group_by=group_by)

    @classmethod
    def all_time(cls, group_by: ReportGroup | None = None) -> Self:
        """Everything up to today."""
        return cls(group_by=group_by)

    def resolve(self, today: date) -> tuple[date | None, date]:
        """Concrete ``(date_from, date_to)`` for the user's ``today``."""
        if self.recent_days is not None:
            return today - timedelta(days=self.recent_days - 1), today
        return self.date_from, self.date_to or today


@dataclass(frozen=True, slots=True, kw_only=True)
class CalibrationBucket:
    """Calibration row: signals given 60-70% actually won 64% (plan §6.10)."""

    lower: float
    upper: float
    count: int
    predicted: float
    actual: float


@dataclass(frozen=True, slots=True, kw_only=True)
class SegmentStat:
    """Metrics of one slice value (e.g. league "Premier League")."""

    key: str
    signals: int
    profit_units: Decimal
    yield_rate: float | None
    clv: float | None
    win_rate: float | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ReportView:
    """Report metrics over settled signals (all signals count, not only placed bets).

    ``yield_rate`` = profit / staked units of the signals; ``roi`` = profit / stake of the
    user's own bets (``None`` without bets); ``clv`` = mean closing-line value;
    ``clv_beat_rate`` = share of signals whose odds beat the closing odds.
    """

    date_from: date | None
    date_to: date
    signals: int
    settled: int
    won: int
    lost: int
    void: int
    profit_units: Decimal
    yield_rate: float | None
    roi: float | None
    clv: float | None
    clv_beat_rate: float | None
    win_rate: float | None
    longest_losing_streak: int
    calibration: tuple[CalibrationBucket, ...]
    segments: tuple[SegmentStat, ...] = ()


@dataclass(frozen=True, slots=True)
class ExportFile:
    """A file for the bot to send as a document (``/eksport``)."""

    filename: str
    content: bytes
    media_type: str = "text/csv"


# --------------------------------------------------------------------------- system status
@dataclass(frozen=True, slots=True, kw_only=True)
class ApiQuota:
    """Today's usage of one provider (per sport when the provider counts per sport)."""

    provider: str
    sport: Sport | None
    used: int
    limit: int
    reserved_manual: int
    manual_reserve: int = 0
    manual_used: int = 0
    """v1.5: ``manual_reserve`` = requests kept back for the user's own actions today,
    ``manual_used`` = how many of them are spent. (``reserved_manual`` is kept for old readers:
    the real service filled it with ``manual_used``, the fake with the reserve.)"""

    @property
    def remaining(self) -> int:
        """Requests left today (never negative)."""
        return max(self.limit - self.used, 0)

    @property
    def manual_left(self) -> int:
        """Requests of the manual reserve still available (never negative)."""
        return max(self.manual_reserve - self.manual_used, 0)


@dataclass(frozen=True, slots=True, kw_only=True)
class LlmQuota:
    """Today's usage of the language model (v1.5): requests and tokens against their caps."""

    requests_used: int
    requests_limit: int
    tokens_used: int
    tokens_limit: int

    @property
    def requests_remaining(self) -> int:
        """Requests left today (never negative)."""
        return max(self.requests_limit - self.requests_used, 0)

    @property
    def tokens_remaining(self) -> int:
        """Tokens left today (never negative)."""
        return max(self.tokens_limit - self.tokens_used, 0)


@dataclass(frozen=True, slots=True, kw_only=True)
class JobStatus:
    """Last run of a scheduled job."""

    name: str
    last_run_at: datetime | None
    ok: bool
    detail: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ErrorEntry:
    """A recent system error (no secrets, already scrubbed)."""

    at: datetime
    component: str
    message: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SystemStatus:
    """``/holat`` (plan §6.12)."""

    quotas: tuple[ApiQuota, ...]
    last_refresh_at: datetime | None
    jobs: tuple[JobStatus, ...]
    recent_errors: tuple[ErrorEntry, ...]
    llm: LlmQuota | None = None


# --------------------------------------------------------------------------- notifications
class UpdateKind(StrEnum):
    """Which automatic recalculation produced a :class:`SignalUpdate` (plan §4 step 5)."""

    BEFORE_24H = "before_24h"
    PREMATCH = "prematch"


class CancelReason(StrEnum):
    """Why a signal stopped being valid without any odds movement."""

    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


@dataclass(frozen=True, slots=True)
class MessageRef:
    """Handle of a message the notifier sent, kept so a later update can reply to or edit it."""

    chat_id: int
    message_id: int


@dataclass(frozen=True, slots=True)
class DeliveryKey:
    """Idempotency key ``(job, day, user_id, subject)`` of one outgoing notification.

    ``day`` is the user's local date for user-facing jobs; ``subject`` is the signal id (as
    text) or another stable id of what is being announced. The pipeline records every key
    it delivers and never delivers the same key twice; the notifier receives it for logging.
    """

    job: str
    day: date
    user_id: int
    subject: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DailyDigest:
    """The morning message (plan §4 step 5): today's candidates, preceded by yesterday's tally.

    v1.7: ``template_name`` is set when the digest is a scheduled saved query (the message is
    labelled with its name; no "yesterday" block). ``loss_limit`` is set when the user's loss
    limit stopped the new signals: ``result`` is then empty and the message is the single
    "Zarar limitiga yetildi" notice of that local day.
    """

    day: date
    result: AnalysisResult
    yesterday: DaySummary | None = None
    template_name: str | None = None
    loss_limit: LossLimitNotice | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalUpdate:
    """A scheduled recalculation: "✅ Kuchda" / "⚠️ Chegaradan pastga tushdi" (plan §4, §6.4)."""

    kind: UpdateKind
    diff: RecalcDiff
    lineups_confirmed: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalCancelled:
    """A signal that can no longer be played (match postponed, cancelled or abandoned)."""

    signal: SignalView
    reason: CancelReason


@dataclass(frozen=True, slots=True, kw_only=True)
class WatchlistEvent:
    """An optional watchlist notification (plan §6.7); ``signal`` is set when one exists."""

    kind: WatchNotification
    fixture: FixtureOption
    detail: str | None = None
    signal: SignalView | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SettledNotice:
    """A signal that was just settled; ``profit_units`` is in the signal's own units."""

    signal: SignalView
    profit_units: Decimal | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AdminAlert:
    """A system error for the administrators (plan §6.12); already scrubbed of secrets."""

    component: str
    message: str
    at: datetime

    def __post_init__(self) -> None:
        """Instants are UTC like everywhere else."""
        _check_utc(self.at, "at")


# --------------------------------------------------------------------------- protocols
class Notifier(Protocol):
    """Pipeline → bot channel for messages nobody asked for (contract v1.2).

    The bot side implements it (Telegram send / edit); the pipeline calls it from scheduled
    jobs. **Quiet hours are applied by the caller, not here:** the pipeline holds back or
    drops notifications that fall into the user's quiet hours (``UserSettingsView
    .quiet_hours``; the daily digest is delayed until they end, update and watchlist
    messages are dropped) and calls the notifier only when sending is allowed. Admin alerts
    are never quiet. A method returns the sent message's :class:`MessageRef`, or ``None`` when
    the message could not be delivered (user blocked the bot); it must not raise for that.
    Idempotency is the caller's job too (see :class:`DeliveryKey`), but implementations
    should tolerate a repeated key.
    """

    async def send_daily_digest(
        self, user_id: int, digest: DailyDigest, *, key: DeliveryKey
    ) -> MessageRef | None:
        """Send the morning message."""
        ...

    async def send_signal_update(
        self,
        user_id: int,
        update: SignalUpdate,
        *,
        key: DeliveryKey,
        reply_to: MessageRef | None = None,
    ) -> MessageRef | None:
        """Send a recalculation result, replying to the signal's earlier message if known."""
        ...

    async def send_signal_cancelled(
        self,
        user_id: int,
        cancelled: SignalCancelled,
        *,
        key: DeliveryKey,
        reply_to: MessageRef | None = None,
    ) -> MessageRef | None:
        """Tell the user a signal was cancelled."""
        ...

    async def send_watchlist_event(
        self, user_id: int, event: WatchlistEvent, *, key: DeliveryKey
    ) -> MessageRef | None:
        """Send a watchlist notification (line-ups, odds move, result)."""
        ...

    async def send_result_settled(
        self, user_id: int, notice: SettledNotice, *, key: DeliveryKey
    ) -> MessageRef | None:
        """Announce a settled signal."""
        ...

    async def alert_admin(self, alert: AdminAlert) -> None:
        """Report a system error to the administrators."""
        ...


@runtime_checkable
class UndeliverableAware(Protocol):
    """Optional companion of a :class:`Notifier` (contract v1.6): "never retry this user".

    A notifier returns ``None`` both for a passing failure (network, flood control) and for a
    user who will not receive anything (blocked the bot, deleted the chat). The pipeline
    retries the first kind with a backoff but must not retry, nor alert the admin about, the
    second: when a notifier also implements this protocol the pipeline adapter asks it after a
    ``None`` answer and treats ``True`` as "handled, undeliverable".
    """

    def is_undeliverable(self, user_id: int) -> bool:
        """True while messages to ``user_id`` are known to be permanently refused."""
        ...


class AnalysisService(Protocol):
    """Analysis and recalculation (plan §6.2-6.4, §6.8)."""

    async def estimate_cost(self, user_id: int, query: AnalysisQuery) -> CostEstimate:
        """Fixtures and API requests the query would need; nothing is spent.

        Raises:
            ValidationFailedError: the window ends beyond the horizon.
        """
        ...

    async def analyze(
        self, user_id: int, query: AnalysisQuery, *, progress: ProgressCallback | None = None
    ) -> AnalysisResult:
        """Run the query and store its signals (re-running returns the same signal ids).

        Only fixtures that have not kicked off produce real signals. Finished fixtures in
        the window produce ``source=retro`` signals (with ``result``) that are never
        recommendations; in-play fixtures are skipped. ``scope=all`` covers
        ``settings.league_ids`` (all tracked leagues when empty) plus watched entities.

        Raises:
            ValidationFailedError: the window ends beyond the horizon or starts before the
                retro look-back (:meth:`DateWindow.ensure_allowed`).
            BudgetExceededError: the manual API reserve cannot cover the request.
            BusyError: the user's previous ``analyze`` has not finished (v1.2).
            ServiceUnavailableError: the data provider or the database is down (v1.2).
        """
        ...

    async def recalc_signal(self, user_id: int, signal_id: SignalId) -> RecalcDiff:
        """Recalculate one signal with fresh odds; at most once per fixture per cooldown.

        When the API reserve is spent, the numbers stay and ``data_as_of`` is set. The
        verdict compares the new numbers with the thresholds the signal was created with
        (the query's probability/EV and the odds range in force then), not today's settings.

        Raises:
            NotFoundError: unknown signal or not the user's.
            CooldownActiveError: recalculated less than :data:`RECALC_COOLDOWN` ago.
            ValidationFailedError: the match has already started.
        """
        ...

    async def recalc_query(
        self, user_id: int, query_id: EntityId, *, progress: ProgressCallback | None = None
    ) -> RecalcBatch:
        """Recalculate every signal of an earlier analysis, skipping cooled-down/started ones.

        Raises:
            NotFoundError: unknown query or not the user's.
        """
        ...

    async def recalc_window(
        self, user_id: int, dates: DateSpec, *, progress: ProgressCallback | None = None
    ) -> RecalcBatch:
        """Recalculate the user's recommended signals with kickoff in ``dates`` (v1.2).

        The query-less ``/qayta``: no ``query_id`` is needed, so it survives a restart. The
        batch's ``query_id`` is a descriptive id of the window. Same skipping rules as
        :meth:`recalc_query`; hidden signals are included, retro ones never are.

        Raises:
            ValidationFailedError: the window ends beyond the horizon.
        """
        ...

    async def recalc_signals(
        self,
        user_id: int,
        signal_ids: Sequence[SignalId],
        *,
        progress: ProgressCallback | None = None,
    ) -> RecalcBatch:
        """Recalculate the given signals (v1.2); unknown or foreign ids are skipped.

        Same skipping rules as :meth:`recalc_query`. Signals of one fixture share one
        recalculation. The batch's ``query_id`` is a descriptive id of the selection.
        """
        ...

    async def fixture_detail(self, fixture_id: EntityId) -> FixtureDetail:
        """The detail card of a fixture.

        Raises:
            NotFoundError: unknown fixture.
        """
        ...


class SignalService(Protocol):
    """Stored signals (⚡ today's signals, card buttons)."""

    async def get_signal(self, user_id: int, signal_id: SignalId) -> SignalView:
        """One signal of the user.

        Raises:
            NotFoundError: unknown signal or not the user's.
        """
        ...

    async def get_signals(
        self, user_id: int, signal_ids: Sequence[SignalId]
    ) -> tuple[SignalView, ...]:
        """Several signals in one call, in the order of ``signal_ids`` (v1.2).

        Unknown ids and other users' signals are omitted rather than failing the batch, so
        a list of stale buttons still renders what exists.
        """
        ...

    async def list_signals(
        self, user_id: int, dates: DateSpec, *, include_hidden: bool = False
    ) -> tuple[SignalView, ...]:
        """The user's non-retro signals with kickoff in ``dates``, ordered by kickoff."""
        ...

    async def hide_signal(self, user_id: int, signal_id: SignalId) -> SignalView:
        """Hide a signal ("🙈 Yashirish"); idempotent.

        Hiding is view-only: the signal keeps counting in reports, results and exports.

        Raises:
            NotFoundError: unknown signal or not the user's.
        """
        ...


class CatalogService(Protocol):
    """Leagues, teams and fixtures for pickers and searches (plan §6.2 step 4, §6.3)."""

    async def list_sports(self) -> tuple[SportOption, ...]:
        """Every :class:`~app.domain.Sport` with its availability, in enum order."""
        ...

    async def list_leagues(self, sport: Sport) -> tuple[LeagueOption, ...]:
        """Tracked leagues of ``sport``, ordered by name."""
        ...

    async def search_teams(
        self, text: str, *, sport: Sport | None = None, limit: int = 10
    ) -> tuple[TeamOption, ...]:
        """Fuzzy team search (typos, Latin/Cyrillic), best match first.

        Raises:
            ValidationFailedError: ``text`` shorter than :data:`MIN_SEARCH_LENGTH` or ``limit``
                outside ``1..MAX_SEARCH_RESULTS``.
        """
        ...

    async def search_fixtures(self, text: str, *, limit: int = 10) -> tuple[FixtureOption, ...]:
        """Upcoming fixtures matching free text such as ``Arsenal - Chelsea``.

        Raises:
            ValidationFailedError: as for :meth:`search_teams`.
        """
        ...

    async def list_fixtures(self, user_id: int, request: FixtureListRequest) -> FixturePage:
        """A page of fixtures in the user's time zone; in-play matches are left out.

        A page past the end is empty, not an error. When both ``league_ids`` and
        ``team_ids`` are given, a fixture matches either; with neither, the user's
        ``settings.league_ids`` apply (all tracked leagues when empty).

        Raises:
            ValidationFailedError: the window ends beyond the horizon.
        """
        ...


class SettingsService(Protocol):
    """User settings (plan §6.5). Unknown users get the defaults."""

    async def get_settings(self, user_id: int) -> UserSettingsView:
        """Current settings."""
        ...

    async def update_settings(self, user_id: int, settings: UserSettingsView) -> UserSettingsView:
        """Replace all settings; the stored ``risk_profile`` is re-derived from the thresholds."""
        ...

    async def apply_risk_profile(self, user_id: int, profile: RiskProfile) -> UserSettingsView:
        """Switch to a preset's thresholds (``custom`` keeps the current thresholds)."""
        ...


class TemplateService(Protocol):
    """Saved queries (plan §6.2 "💾 Shablonlar")."""

    async def save_template(self, user_id: int, draft: TemplateDraft) -> TemplateView:
        """Save; a template with the same name is replaced (keeps its id)."""
        ...

    async def list_templates(self, user_id: int) -> tuple[TemplateView, ...]:
        """All templates of the user, ordered by name."""
        ...

    async def delete_template(self, user_id: int, template_id: TemplateId) -> None:
        """Delete.

        Raises:
            NotFoundError: unknown template or not the user's.
        """
        ...


class WatchlistService(Protocol):
    """Followed teams and leagues (plan §6.7)."""

    async def list_watchlist(self, user_id: int) -> tuple[WatchItem, ...]:
        """All entries, ordered by name."""
        ...

    async def add_watch(self, user_id: int, target: WatchTarget) -> WatchItem:
        """Follow (idempotent).

        Raises:
            NotFoundError: unknown team or league.
        """
        ...

    async def remove_watch(self, user_id: int, target: WatchTarget) -> bool:
        """Unfollow; ``False`` if it was not followed."""
        ...

    async def set_notifications(
        self, user_id: int, target: WatchTarget, notifications: frozenset[WatchNotification]
    ) -> WatchItem:
        """Choose which notifications the entry sends.

        Raises:
            NotFoundError: the target is not on the watchlist.
        """
        ...


class BetService(Protocol):
    """The user's own bets and bank (plan §6.9). Settlement is automatic."""

    async def place_bet(self, user_id: int, draft: BetDraft) -> BetView:
        """Record a bet on a signal.

        Idempotent per ``(user, draft.client_token)`` (v1.2): repeating a token returns the
        bet recorded first and creates nothing. Without a token every call is a new bet.

        Raises:
            NotFoundError: unknown signal or not the user's.
            ValidationFailedError: no stake given and none can be derived (no unit set or a
                zero-stake signal), or a retro signal (a look-back, not a bet).
        """
        ...

    async def list_bets(
        self, user_id: int, *, open_only: bool = False, limit: int = 20
    ) -> tuple[BetView, ...]:
        """Most recent bets first."""
        ...

    async def bank_summary(self, user_id: int) -> BankView:
        """Balance, monthly P/L, loss-limit state and the balance history."""
        ...


class ResultService(Protocol):
    """Result marking and feedback (plan §6.13 A)."""

    async def pending_results(self, user_id: int) -> tuple[SignalView, ...]:
        """Recommended signals (hidden ones too) whose match is over without a result."""
        ...

    async def mark_result(self, user_id: int, request: ResultMarkRequest) -> SignalView:
        """Set or correct a result by hand; a manual mark overrides the API result.

        Raises:
            NotFoundError: unknown signal or not the user's.
            ValidationFailedError: the match has not started yet, or a retro signal.
        """
        ...

    async def add_note(self, user_id: int, signal_id: SignalId, text: str) -> None:
        """Attach a note ("💬 Izoh") used as context in later analyses.

        Raises:
            NotFoundError: unknown signal or not the user's.
            ValidationFailedError: blank or longer than :data:`MAX_NOTE_LENGTH`.
        """
        ...

    async def day_summary(self, user_id: int, day: date) -> DaySummary:
        """Summary of the recommended signals that kicked off on ``day`` (local date)."""
        ...


class ReportService(Protocol):
    """Performance reports (plan §6.10) over recommended, non-retro signals.

    Hidden signals count; no-value and retro signals never do (no selection bias).
    """

    async def report(self, user_id: int, request: ReportRequest) -> ReportView:
        """Metrics, calibration table and the optional slice; ROI uses all the user's bets."""
        ...

    async def export(self, user_id: int) -> ExportFile:
        """All recommended signals with results as CSV (``/eksport``)."""
        ...

    async def export_bets(self, user_id: int) -> ExportFile:
        """All of the user's bets, open and settled, as CSV (v1.6)."""
        ...


class ExpressService(Protocol):
    """Express builder (plan §6.11, v1.6)."""

    async def evaluate(self, user_id: int, signal_ids: Sequence[SignalId]) -> ExpressView:
        """Combined odds, probability and EV of ``signal_ids``.

        Raises:
            ValidationFailedError: not :data:`MIN_EXPRESS_LEGS`..:data:`MAX_EXPRESS_LEGS`
                distinct signals, two picks of one match (correlated markets are never
                combined), or a pick that is retro, cancelled, settled or already started.
            NotFoundError: unknown signal or not the user's.
        """
        ...


class SystemStatusService(Protocol):
    """System health for ``/holat`` (plan §6.12)."""

    async def system_status(self) -> SystemStatus:
        """API quotas, last refresh, job runs and recent errors."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class Services:
    """Everything the bot needs, injected at startup (fake now, real services in F4)."""

    analysis: AnalysisService
    signals: SignalService
    catalog: CatalogService
    settings: SettingsService
    templates: TemplateService
    watchlist: WatchlistService
    bets: BetService
    results: ResultService
    reports: ReportService
    status: SystemStatusService
    express: ExpressService
