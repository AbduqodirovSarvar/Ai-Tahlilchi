"""Value types passed between the pipeline and the service layer (no I/O, immutable)."""

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.db.enums import QueryScope, SignalSource, SortOrder
from app.db.repo import SignalRecord, Thresholds
from app.domain import MarketType, Sport
from app.pipeline.provider import MANUAL, Purpose


@dataclass(frozen=True, slots=True, kw_only=True)
class AnalysisRequest:
    """One analysis run for one user: a UTC window, thresholds, scope and caps.

    The service layer builds it from the user's settings and the :class:`AnalysisQuery`
    (resolving local dates to ``[start, end)``); the pipeline never sees local dates.
    ``league_ids``/``team_ids``/``fixture_ids`` are database ids of the chosen scope;
    ``default_league_ids`` are the user's settings leagues (empty = every tracked league)
    that scope ``all`` uses. ``watch_*`` are the user's followed entities.
    """

    user_id: int
    start: datetime
    end: datetime
    zone: ZoneInfo
    sports: frozenset[Sport]
    markets: frozenset[MarketType]
    min_probability: float
    min_ev: float
    odds_min: float
    odds_max: float
    scope: QueryScope = QueryScope.ALL
    league_ids: frozenset[int] = frozenset()
    team_ids: frozenset[int] = frozenset()
    fixture_ids: frozenset[int] = frozenset()
    default_league_ids: frozenset[int] = frozenset()
    watch_league_ids: frozenset[int] = frozenset()
    watch_team_ids: frozenset[int] = frozenset()
    max_signals: int | None = None
    per_day: bool = False
    sort: SortOrder = SortOrder.EV
    include_no_value: bool = False
    source: SignalSource = SignalSource.ANALYSIS
    bank: Decimal | None = None
    unit: Decimal | None = None
    prematch_minutes: int | None = None
    purpose: Purpose = MANUAL
    fetch: bool = True
    skip_retro: bool = False
    """Leave out fixtures that already started (the daily digest has no use for look-backs)."""

    @property
    def thresholds(self) -> Thresholds:
        """The value thresholds a signal created by this request remembers."""
        return Thresholds(self.min_probability, self.min_ev, self.odds_min, self.odds_max)


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalCard:
    """A stored signal plus what is known only while the pipeline handles it.

    ``no_value``: below the request's thresholds (shown for a custom analysis or the
    watchlist). ``data_as_of``: the numbers are older than a recalculation wanted because
    the API budget was spent. ``risk_flags`` are stable codes, not user text.
    """

    record: SignalRecord
    bookmaker: str
    no_value: bool
    watchlist: bool = False
    risk_flags: tuple[str, ...] = ()
    data_as_of: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AnalysisOutcome:
    """What :meth:`AnalysisEngine.run` produced (``cards`` sorted by the request's key)."""

    cards: tuple[SignalCard, ...]
    fixtures_analyzed: int
    matched: int
    requested: int | None
    api_requests_used: int
    failed_fixtures: int = 0
    skipped_no_price: int = 0
    """Fixtures left out because the user's bookmaker has no (fresh) price for them."""
    skipped_stale_price: int = 0
    """Fixtures left out because the bookmaker's price is too old to decide on."""
    data_stale: bool = False
    """Provider data the run wanted could not be refreshed (budget spent or provider down)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DataPlan:
    """What an analysis would need from the provider (nothing is spent by planning)."""

    fixtures_found: int
    api_requests: int
    budget_remaining: int


@dataclass(frozen=True, slots=True)
class QuietWindow:
    """Local time span without notifications; may wrap midnight."""

    start: time
    end: time

    def contains(self, moment: time) -> bool:
        """True when ``moment`` (local wall-clock time) is inside the quiet span."""
        if self.start < self.end:
            return self.start <= moment < self.end
        return moment >= self.start or moment < self.end


@dataclass(frozen=True, slots=True, kw_only=True)
class UserSchedule:
    """When a user wants to hear from the system, in their own time zone."""

    user_id: int
    zone: ZoneInfo
    send_time: time
    quiet: QuietWindow | None = None
    prematch_minutes: int | None = None

    def local_day(self, now: datetime) -> date:
        """The user's calendar date at ``now``."""
        return now.astimezone(self.zone).date()

    def is_quiet(self, now: datetime) -> bool:
        """True when ``now`` falls into the user's quiet hours."""
        return self.quiet is not None and self.quiet.contains(
            now.astimezone(self.zone).time().replace(tzinfo=None)
        )
