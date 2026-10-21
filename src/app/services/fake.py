"""Deterministic in-memory implementation of every service protocol (contract #5).

Lets the bot be built and tested before the pipeline exists. Behaviour follows the contract
(``docs/contracts.md`` §4): same filters, recommendation and retro rules, cooldown, API
budget, idempotent re-analysis, progress callbacks and errors, over a hash-generated sample
world (``_fake_world``) that follows the injected clock. No I/O, no randomness, no ``sleep``.

Usage::

    services = create_fake_services()  # or create_fake_services(clock=my_test_clock)
    result = await services.analysis.analyze(user_id, AnalysisQuery(dates=DateSpec.today()))
"""

import csv
import io
import itertools
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Final, Self
from zoneinfo import ZoneInfo

from app.domain import Sport, normalize_name
from app.logging import get_logger
from app.services._fake_world import (
    EXPERIMENTAL_SPORTS,
    SPORT_MARKETS,
    FakeFixture,
    FakeQuote,
    World,
    build_detail,
    build_world,
    drifted_odds,
    llm_note,
    settle,
    unit_interval,
)
from app.services.errors import (
    BudgetExceededError,
    BusyError,
    CooldownActiveError,
    NotFoundError,
    ValidationFailedError,
)
from app.services.interfaces import (
    DEFAULT_TIMEZONE,
    MANUAL_RESERVE_SHARE,
    MAX_NOTE_LENGTH,
    MAX_SEARCH_RESULTS,
    MIN_SEARCH_LENGTH,
    PRELIMINARY_AFTER,
    RECALC_COOLDOWN,
    AdminAlert,
    AnalysisProgress,
    AnalysisQuery,
    AnalysisResult,
    AnalysisStage,
    ApiQuota,
    BankPoint,
    BankView,
    BetDraft,
    BetId,
    BetView,
    ConfidenceLevel,
    CostEstimate,
    DailyDigest,
    DateSpec,
    DateWindow,
    DaySummary,
    DeliveryKey,
    EntityId,
    ExportFile,
    FixtureDetail,
    FixtureListRequest,
    FixtureOption,
    FixturePage,
    JobStatus,
    LeagueOption,
    MessageRef,
    ProgressCallback,
    RecalcBatch,
    RecalcDiff,
    RecalcVerdict,
    ReportRequest,
    ReportView,
    ResultMark,
    ResultMarkRequest,
    RiskProfile,
    ScopeKind,
    Services,
    SettledNotice,
    SignalCancelled,
    SignalId,
    SignalNumbers,
    SignalSource,
    SignalState,
    SignalUpdate,
    SignalView,
    SortKey,
    SportOption,
    SystemStatus,
    TeamOption,
    TemplateDraft,
    TemplateId,
    TemplateView,
    UserSettingsView,
    Verdict,
    WatchItem,
    WatchKind,
    WatchlistEvent,
    WatchNotification,
    WatchTarget,
    infer_risk_profile,
)
from app.services.report_math import SignalOutcome, build_report, day_summary, profit_units

type Clock = Callable[[], datetime]
"""Returns the current UTC instant; injected so tests control time."""

FAKE_DAILY_LIMIT: Final = 100
FAKE_AUTOMATIC_USAGE: Final = 40
FAKE_MANUAL_RESERVE: Final = round(FAKE_DAILY_LIMIT * MANUAL_RESERVE_SHARE)
CACHE_TTL: Final = timedelta(minutes=10)
"""The same provider data is not requested again within this period (plan §9)."""
RECALC_REQUESTS: Final = 1
REFRESH_TIME: Final = time(8, 30)
PROVIDER: Final = "api-football"
CAUTION_SHARE: Final = 0.25
_CONFIDENCE_LEVELS: Final = (ConfidenceLevel.HIGH, ConfidenceLevel.MEDIUM, ConfidenceLevel.LOW)
_STAKE_LADDER: Final = ((0.10, Decimal("1.5")), (0.05, Decimal(1)), (0.0, Decimal("0.5")))
_FUZZY_THRESHOLD: Final = 0.6
_MAX_BET_LIST: Final = 100
_CLV_FLOOR: Final = -0.03
_CLV_SPAN: Final = 0.09
_NEVER: Final = datetime.min.replace(tzinfo=UTC)

_log = get_logger(__name__)


def system_clock() -> datetime:
    """Real UTC time, for running the bot against the fake by hand."""
    return datetime.now(UTC)


async def _report(
    progress: ProgressCallback | None, stage: AnalysisStage, done: int, total: int
) -> None:
    """Call the progress callback; its failures are logged and never abort the job."""
    if progress is None:
        return
    try:
        await progress(AnalysisProgress(stage=stage, done=done, total=total))
    except Exception as error:
        _log.warning("progress_callback_failed", stage=stage, error=repr(error))


@dataclass(frozen=True, slots=True, kw_only=True)
class _Thresholds:
    """The value thresholds a signal was judged against when it was created/recommended."""

    min_probability: float
    min_ev: float
    odds_min: float
    odds_max: float

    @classmethod
    def of(cls, query: AnalysisQuery, settings: UserSettingsView) -> Self:
        return cls(
            min_probability=query.min_probability,
            min_ev=query.min_ev,
            odds_min=settings.odds_min,
            odds_max=settings.odds_max,
        )

    def passes(self, quote: FakeQuote) -> bool:
        """Probability and EV thresholds plus the odds range."""
        return (
            quote.probability >= self.min_probability
            and quote.ev >= self.min_ev
            and self.odds_min <= quote.odds <= self.odds_max
        )


@dataclass(slots=True, kw_only=True)
class _StoredSignal:
    """Mutable server-side record of a signal (the fake's ``signals`` row)."""

    signal_id: SignalId
    user_id: int
    fixture: FakeFixture
    quote: FakeQuote
    odds: float
    source: SignalSource
    thresholds: _Thresholds
    risk_profile: RiskProfile
    no_value: bool
    watchlist: bool
    recommended_at: datetime | None = None
    version: int = 1
    state: SignalState = SignalState.ACTIVE
    data_as_of: datetime | None = None
    manual_mark: ResultMark | None = None
    cashout_units: Decimal | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ev(self) -> float:
        """EV at the current odds."""
        return self.quote.probability * self.odds - 1.0

    @property
    def key(self) -> str:
        """Stable hash key of the selection."""
        return f"{self.fixture.fixture_id}|{self.quote.market}|{self.quote.selection}"

    @property
    def retro(self) -> bool:
        """True for a look-back at a match that had kicked off."""
        return self.source is SignalSource.RETRO

    @property
    def counts(self) -> bool:
        """Recommended and not retro: the signal belongs in reports (hidden or not)."""
        return self.recommended_at is not None and not self.retro


@dataclass(slots=True, kw_only=True)
class _StoredBet:
    bet_id: BetId
    user_id: int
    signal_id: SignalId
    odds_taken: float
    stake: Decimal
    placed_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class _Pick:
    fixture: FakeFixture
    quote: FakeQuote
    passes: bool
    watchlist: bool = False


class FakeState:
    """Shared in-memory store of all fake services (one per :func:`create_fake_services`)."""

    def __init__(self, clock: Clock) -> None:
        """The sample world is generated around the clock's local date and follows it."""
        self.clock = clock
        self._zone = ZoneInfo(DEFAULT_TIMEZONE)
        self._anchor: date | None = None
        self._world: World | None = None
        self.fixtures: dict[EntityId, FakeFixture] = {}
        self.settings: dict[int, UserSettingsView] = {}
        self.signals: dict[SignalId, _StoredSignal] = {}
        self.signal_ids: dict[tuple[int, bool, str], SignalId] = {}
        self.queries: dict[EntityId, tuple[int, tuple[SignalId, ...]]] = {}
        self.recalc_at: dict[EntityId, datetime] = {}
        self.fetched_at: dict[str, datetime] = {}
        self.manual_used: dict[date, int] = {}
        self.templates: dict[int, dict[str, TemplateView]] = {}
        self.watchlist: dict[int, dict[WatchTarget, WatchItem]] = {}
        self.bets: dict[BetId, _StoredBet] = {}
        self.bet_tokens: dict[tuple[int, str], BetId] = {}
        self.busy: set[int] = set()
        self.next_id = itertools.count(1)

    def now(self) -> datetime:
        """Current UTC instant."""
        return self.clock()

    @property
    def world(self) -> World:
        """Sample world around the clock's current date; regenerated when the day changes.

        Fixtures seen earlier stay resolvable (``self.fixtures``), so stored signals and
        detail cards keep working in long manual sessions.
        """
        today = self.now().astimezone(self._zone).date()
        if self._world is None or today != self._anchor:
            self._world = build_world(today, self._zone)
            self._anchor = today
            self.fixtures.update({fixture.fixture_id: fixture for fixture in self._world.fixtures})
        return self._world

    def settings_for(self, user_id: int) -> UserSettingsView:
        """Stored settings or the defaults."""
        return self.settings.get(user_id, UserSettingsView())

    def local_today(self, zone: ZoneInfo) -> date:
        """Today in ``zone``."""
        return self.now().astimezone(zone).date()

    def resolve(self, dates: DateSpec, settings: UserSettingsView) -> DateWindow:
        """Concrete window in the user's zone, checked against the horizon."""
        today = self.local_today(settings.zone)
        window = dates.resolve(today)
        window.ensure_allowed(today)
        return window

    def fixture(self, fixture_id: EntityId) -> FakeFixture:
        """Fixture by id."""
        _ = self.world
        try:
            return self.fixtures[fixture_id]
        except KeyError:
            raise NotFoundError("fixture", fixture_id) from None

    def signal(self, user_id: int, signal_id: SignalId) -> _StoredSignal:
        """Signal by id; other users' signals do not exist for this user."""
        stored = self.signals.get(signal_id)
        if stored is None or stored.user_id != user_id:
            raise NotFoundError("signal", signal_id)
        return stored

    def user_signals(self, user_id: int) -> list[_StoredSignal]:
        """All signals of the user, by kickoff."""
        rows = [stored for stored in self.signals.values() if stored.user_id == user_id]
        rows.sort(key=lambda stored: (stored.fixture.kickoff, stored.signal_id))
        return rows

    def fixtures_in(
        self, window: DateWindow, zone: ZoneInfo, sports: frozenset[Sport]
    ) -> list[FakeFixture]:
        """Fixtures with local kickoff date in ``window``; in-play matches are left out."""
        now = self.now()
        return [
            fixture
            for fixture in self.world.fixtures
            if fixture.sport in sports
            and window.contains(fixture.kickoff, zone)
            and not fixture.in_play(now)
        ]

    def watched(self, user_id: int, fixture: FakeFixture) -> bool:
        """True when a team or the league of ``fixture`` is on the user's watchlist."""
        targets = self.watchlist.get(user_id, {})
        candidates = (
            WatchTarget(WatchKind.TEAM, fixture.home.team_id),
            WatchTarget(WatchKind.TEAM, fixture.away.team_id),
            WatchTarget(WatchKind.LEAGUE, fixture.league.league_id),
        )
        return any(target in targets for target in candidates)

    # ------------------------------------------------------------------ API budget
    def budget_remaining(self) -> int:
        """Manual API requests left today."""
        return FAKE_MANUAL_RESERVE - self.manual_used.get(self.local_today(self._zone), 0)

    def uncached(self, keys: Iterable[str]) -> list[str]:
        """Provider resources not fetched within :data:`CACHE_TTL`."""
        now = self.now()
        return sorted({key for key in keys if now - self.fetched_at.get(key, _NEVER) >= CACHE_TTL})

    def spend(self, requests: int, keys: Iterable[str] = ()) -> None:
        """Count ``requests`` against today's manual reserve and cache ``keys``."""
        today = self.local_today(self._zone)
        self.manual_used[today] = self.manual_used.get(today, 0) + requests
        now = self.now()
        for key in keys:
            self.fetched_at[key] = now

    def refreshed_at(self) -> datetime:
        """Last morning refresh (08:30 local), the "data as of" time of cached numbers."""
        now = self.now()
        refresh = datetime.combine(now.astimezone(self._zone).date(), REFRESH_TIME, self._zone)
        if refresh > now:
            refresh -= timedelta(days=1)
        return refresh.astimezone(UTC)

    # ------------------------------------------------------------------ signals
    def store(self, user_id: int, pick: _Pick, query: AnalysisQuery) -> _StoredSignal:
        """Insert or update the user's signal for this selection (stable id).

        Recommendation is sticky: once recommended a signal stays recommended; a no-value
        signal that passes a later query is recommended *at that later time*.
        """
        retro = pick.fixture.started(self.now())
        selection_key = f"{pick.fixture.fixture_id}|{pick.quote.market}|{pick.quote.selection}"
        existing = self.signal_ids.get((user_id, retro, selection_key))
        settings = self.settings_for(user_id)
        if existing is None:
            stored = self._create(user_id, pick, query, retro=retro)
            self.signal_ids[(user_id, retro, selection_key)] = stored.signal_id
        else:
            stored = self.signals[existing]
            stored.watchlist = stored.watchlist or pick.watchlist
        if pick.passes and not retro and stored.recommended_at is None:
            stored.recommended_at = self.now()
            stored.no_value = False
            stored.thresholds = _Thresholds.of(query, settings)
        return stored

    def _create(
        self, user_id: int, pick: _Pick, query: AnalysisQuery, *, retro: bool
    ) -> _StoredSignal:
        settings = self.settings_for(user_id)
        stored = _StoredSignal(
            signal_id=next(self.next_id),
            user_id=user_id,
            fixture=pick.fixture,
            quote=pick.quote,
            odds=pick.quote.odds,
            source=_source(query, pick, retro=retro),
            thresholds=_Thresholds.of(query, settings),
            risk_profile=settings.risk_profile,
            no_value=not pick.passes,
            watchlist=pick.watchlist,
        )
        self.signals[stored.signal_id] = stored
        return stored

    def result_of(self, stored: _StoredSignal) -> ResultMark | None:
        """Manual mark, else the "API" settlement once the match is over and reported."""
        if stored.manual_mark is not None:
            return stored.manual_mark
        fixture = stored.fixture
        if fixture.finished(self.now()) and fixture.result_reported:
            return settle(stored.quote, fixture.home_score, fixture.away_score)
        return None

    def view(self, stored: _StoredSignal) -> SignalView:
        """Card of a stored signal as of now."""
        fixture, quote = stored.fixture, stored.quote
        preliminary = fixture.kickoff - self.now() > PRELIMINARY_AFTER
        verdict = _verdict(stored)
        return SignalView(
            signal_id=stored.signal_id,
            fixture_id=fixture.fixture_id,
            sport=fixture.sport,
            league=fixture.league.name,
            home_team=fixture.home.name,
            away_team=fixture.away.name,
            kickoff=fixture.kickoff,
            market=quote.market,
            selection=quote.selection,
            odds=stored.odds,
            bookmaker="pinnacle",
            probability=quote.probability,
            ev=stored.ev,
            confidence=_confidence(stored.key, preliminary=preliminary),
            stake_units=_stake_units(stored.ev, no_value=stored.no_value),
            llm_verdict=verdict,
            llm_note=None if verdict is None else llm_note(stored.key),
            risk_flags=("rotatsiya",) if verdict is Verdict.CAUTION else (),
            preliminary=preliminary,
            no_value=stored.no_value,
            watchlist=stored.watchlist,
            experimental=fixture.sport in EXPERIMENTAL_SPORTS,
            state=stored.state,
            result=self.result_of(stored),
            version=stored.version,
            data_as_of=stored.data_as_of,
            source=stored.source,
            recommended_at=stored.recommended_at,
        )

    def outcomes(self, user_id: int) -> list[SignalOutcome]:
        """Recommended, non-retro signals (hidden included) with their profit and CLV."""
        rows = []
        for stored in self.user_signals(user_id):
            if not stored.counts:
                continue
            view = self.view(stored)
            rows.append(
                SignalOutcome(
                    view=view,
                    profit_units=profit_units(view, stored.cashout_units),
                    clv=None if view.result is None else _clv(stored.key),
                    risk_profile=stored.risk_profile,
                )
            )
        return rows


def _source(query: AnalysisQuery, pick: _Pick, *, retro: bool) -> SignalSource:
    if retro:
        return SignalSource.RETRO
    if query.include_no_value:
        return SignalSource.CUSTOM
    return SignalSource.WATCHLIST if pick.watchlist else SignalSource.ANALYSIS


def _confidence(key: str, *, preliminary: bool) -> ConfidenceLevel:
    """Stable level; preliminary signals are one level lower (plan §6.2)."""
    index = int(unit_interval(key, "confidence") * len(_CONFIDENCE_LEVELS))
    if preliminary:
        index = min(index + 1, len(_CONFIDENCE_LEVELS) - 1)
    return _CONFIDENCE_LEVELS[index]


def _stake_units(ev: float, *, no_value: bool) -> Decimal:
    """Flat stake ladder by EV; nothing is staked on no-value or negative-EV selections."""
    if no_value or ev < 0:
        return Decimal(0)
    return next(stake for threshold, stake in _STAKE_LADDER if ev >= threshold)


def _verdict(stored: _StoredSignal) -> Verdict | None:
    """Only value signals are reviewed by the LLM."""
    if stored.no_value:
        return None
    caution = unit_interval(stored.key, "verdict") < CAUTION_SHARE
    return Verdict.CAUTION if caution else Verdict.CONFIRM


def _clv(key: str) -> float:
    return _CLV_FLOOR + _CLV_SPAN * unit_interval(key, "clv")


def _numbers(view: SignalView) -> SignalNumbers:
    return SignalNumbers(
        odds=view.odds,
        probability=view.probability,
        ev=view.ev,
        stake_units=view.stake_units,
        confidence=view.confidence,
    )


def _sort_key(sort: SortKey) -> Callable[[_Pick], tuple[object, ...]]:
    def key(pick: _Pick) -> tuple[object, ...]:
        quote = pick.quote
        tie_break = (pick.fixture.fixture_id, str(quote.market), quote.selection.value)
        match sort:
            case SortKey.EV:
                return (-quote.ev, pick.fixture.kickoff, *tie_break)
            case SortKey.PROBABILITY:
                return (-quote.probability, pick.fixture.kickoff, *tie_break)
            case SortKey.KICKOFF:
                return (pick.fixture.kickoff, -quote.ev, *tie_break)

    return key


def _cap(picks: list[_Pick], query: AnalysisQuery, zone: ZoneInfo) -> list[_Pick]:
    """Apply ``max_signals`` in total or per local day, keeping the sort order."""
    if query.max_signals is None:
        return picks
    if not query.per_day:
        return picks[: query.max_signals]
    per_day: dict[date, int] = {}
    kept = []
    for pick in picks:
        day = pick.fixture.kickoff.astimezone(zone).date()
        if per_day.get(day, 0) < query.max_signals:
            per_day[day] = per_day.get(day, 0) + 1
            kept.append(pick)
    return kept


def _resource_keys(
    fixtures: Sequence[FakeFixture], query: AnalysisQuery, now: datetime
) -> list[str]:
    """Provider resources an analysis needs: odds per league, injuries for near fixtures."""
    upcoming = [fixture for fixture in fixtures if not fixture.started(now)]
    keys = {f"odds:{fixture.league.league_id}" for fixture in upcoming}
    keys |= {
        f"injuries:{fixture.fixture_id}"
        for fixture in upcoming
        if fixture.kickoff - now <= PRELIMINARY_AFTER
    }
    if upcoming and query.scope is ScopeKind.TEAMS:
        keys |= {f"team:{team_id}" for team_id in query.scope_ids}
    return sorted(keys)


@dataclass(frozen=True, slots=True, kw_only=True)
class _Plan:
    window: DateWindow
    fixtures: list[FakeFixture]
    keys: list[str]


class FakeAnalysisService:
    """Fake :class:`~app.services.interfaces.AnalysisService`."""

    def __init__(self, state: FakeState) -> None:
        """Share ``state`` with the other fake services."""
        self._state = state

    def _in_scope(self, user_id: int, fixture: FakeFixture, query: AnalysisQuery) -> bool:
        ids = query.scope_ids
        match query.scope:
            case ScopeKind.ALL:
                leagues = self._state.settings_for(user_id).league_ids
                in_leagues = not leagues or fixture.league.league_id in leagues
                return in_leagues or self._state.watched(user_id, fixture)
            case ScopeKind.LEAGUES:
                return fixture.league.league_id in ids
            case ScopeKind.TEAMS:
                return fixture.home.team_id in ids or fixture.away.team_id in ids
            case ScopeKind.FIXTURES:
                return fixture.fixture_id in ids

    def _plan(self, user_id: int, query: AnalysisQuery) -> _Plan:
        settings = self._state.settings_for(user_id)
        window = self._state.resolve(query.dates, settings)
        fixtures = [
            fixture
            for fixture in self._state.fixtures_in(window, settings.zone, query.sports)
            if self._in_scope(user_id, fixture, query)
        ]
        keys = self._state.uncached(_resource_keys(fixtures, query, self._state.now()))
        return _Plan(window=window, fixtures=fixtures, keys=keys)

    async def estimate_cost(self, user_id: int, query: AnalysisQuery) -> CostEstimate:
        """See :meth:`AnalysisService.estimate_cost`."""
        plan = self._plan(user_id, query)
        return CostEstimate(
            window=plan.window,
            fixtures_found=len(plan.fixtures),
            api_requests=len(plan.keys),
            budget_remaining=self._state.budget_remaining(),
        )

    async def analyze(
        self, user_id: int, query: AnalysisQuery, *, progress: ProgressCallback | None = None
    ) -> AnalysisResult:
        """See :meth:`AnalysisService.analyze` (one at a time per user)."""
        if user_id in self._state.busy:
            raise BusyError(user_id)
        self._state.busy.add(user_id)
        try:
            return await self._analyze(user_id, query, progress)
        finally:
            self._state.busy.discard(user_id)

    async def _analyze(
        self, user_id: int, query: AnalysisQuery, progress: ProgressCallback | None
    ) -> AnalysisResult:
        plan = self._plan(user_id, query)
        available = self._state.budget_remaining()
        if len(plan.keys) > available:
            raise BudgetExceededError(needed=len(plan.keys), available=available)
        self._state.spend(len(plan.keys), plan.keys)
        total = len(plan.fixtures)
        await _report(progress, AnalysisStage.COLLECTING, 0, total)
        regular: list[_Pick] = []
        watched: list[_Pick] = []
        for done, fixture in enumerate(plan.fixtures, start=1):
            picks = self._picks(user_id, fixture, query)
            is_watched = query.scope is ScopeKind.ALL and any(pick.watchlist for pick in picks)
            (watched if is_watched else regular).extend(picks)
            await _report(progress, AnalysisStage.ANALYSING, done, total)
        await _report(progress, AnalysisStage.REVIEWING, total, total)
        sort_key = _sort_key(query.sort)
        regular.sort(key=sort_key)
        zone = self._state.settings_for(user_id).zone
        kept = sorted([*_cap(regular, query, zone), *watched], key=sort_key)
        stored = [self._state.store(user_id, pick, query) for pick in kept]
        query_id = f"q{next(self._state.next_id)}"
        self._state.queries[query_id] = (user_id, tuple(item.signal_id for item in stored))
        await _report(progress, AnalysisStage.FINISHED, total, total)
        return AnalysisResult(
            query_id=query_id,
            window=plan.window,
            signals=tuple(self._state.view(item) for item in stored),
            fixtures_analyzed=total,
            matched=len(regular),
            requested=query.max_signals,
            api_requests_used=len(plan.keys),
            retro=plan.window.is_retro(self._state.local_today(zone)),
        )

    def _picks(self, user_id: int, fixture: FakeFixture, query: AnalysisQuery) -> list[_Pick]:
        """Best passing selection per fixture; every selection for custom analyses.

        A watched fixture (scope ``all``) without a passing selection still yields its
        best-EV selection, flagged no-value.
        """
        thresholds = _Thresholds.of(query, self._state.settings_for(user_id))
        watched = query.scope is ScopeKind.ALL and self._state.watched(user_id, fixture)
        picks = [
            _Pick(fixture=fixture, quote=quote, passes=thresholds.passes(quote), watchlist=watched)
            for quote in fixture.quotes
            if quote.market.type in query.markets
        ]
        if query.include_no_value:
            return picks
        passing = [pick for pick in picks if pick.passes]
        if passing:
            return [min(passing, key=_sort_key(query.sort))]
        if watched and picks:
            return [max(picks, key=lambda pick: pick.quote.ev)]
        return []

    async def recalc_signal(self, user_id: int, signal_id: SignalId) -> RecalcDiff:
        """See :meth:`AnalysisService.recalc_signal`."""
        stored = self._state.signal(user_id, signal_id)
        if stored.fixture.started(self._state.now()):
            raise ValidationFailedError("signal_id", "the match has already started")
        self._claim(stored.fixture.fixture_id)
        return self._recalc(stored)

    async def recalc_query(
        self, user_id: int, query_id: EntityId, *, progress: ProgressCallback | None = None
    ) -> RecalcBatch:
        """See :meth:`AnalysisService.recalc_query`."""
        owner, signal_ids = self._state.queries.get(query_id, (None, ()))
        if owner != user_id:
            raise NotFoundError("query", query_id)
        return await self._recalc_batch(user_id, query_id, signal_ids, progress)

    async def recalc_window(
        self, user_id: int, dates: DateSpec, *, progress: ProgressCallback | None = None
    ) -> RecalcBatch:
        """See :meth:`AnalysisService.recalc_window`."""
        settings = self._state.settings_for(user_id)
        window = self._state.resolve(dates, settings)
        signal_ids = tuple(
            stored.signal_id
            for stored in self._state.user_signals(user_id)
            if stored.counts and window.contains(stored.fixture.kickoff, settings.zone)
        )
        query_id = f"w:{window.date_from}:{window.date_to}"
        return await self._recalc_batch(user_id, query_id, signal_ids, progress)

    async def recalc_signals(
        self,
        user_id: int,
        signal_ids: Sequence[SignalId],
        *,
        progress: ProgressCallback | None = None,
    ) -> RecalcBatch:
        """See :meth:`AnalysisService.recalc_signals`."""
        own = tuple(
            dict.fromkeys(
                signal_id
                for signal_id in signal_ids
                if (stored := self._state.signals.get(signal_id)) is not None
                and stored.user_id == user_id
                and not stored.retro
            )
        )
        return await self._recalc_batch(user_id, f"s:{len(own)}", own, progress)

    async def _recalc_batch(
        self,
        user_id: int,
        query_id: EntityId,
        signal_ids: Sequence[SignalId],
        progress: ProgressCallback | None,
    ) -> RecalcBatch:
        """Recalculate ``signal_ids`` once per fixture; cooled-down and started ones are skipped."""
        diffs, cooled, started = [], [], []
        claimed: set[EntityId] = set()
        for done, signal_id in enumerate(signal_ids, start=1):
            stored = self._state.signal(user_id, signal_id)
            fixture_id = stored.fixture.fixture_id
            if stored.fixture.started(self._state.now()):
                started.append(signal_id)
            elif fixture_id in claimed or self._try_claim(fixture_id):
                claimed.add(fixture_id)
                diffs.append(self._recalc(stored))
            else:
                cooled.append(signal_id)
            await _report(progress, AnalysisStage.ANALYSING, done, len(signal_ids))
        await _report(progress, AnalysisStage.FINISHED, len(signal_ids), len(signal_ids))
        return RecalcBatch(
            query_id=query_id,
            diffs=tuple(diffs),
            skipped_cooldown=tuple(cooled),
            skipped_started=tuple(started),
        )

    def _claim(self, fixture_id: EntityId) -> None:
        """Start the cooldown for ``fixture_id`` or report how long is left."""
        now = self._state.now()
        last = self._state.recalc_at.get(fixture_id)
        if last is not None and now - last < RECALC_COOLDOWN:
            raise CooldownActiveError(retry_after=RECALC_COOLDOWN - (now - last))
        self._state.recalc_at[fixture_id] = now

    def _try_claim(self, fixture_id: EntityId) -> bool:
        try:
            self._claim(fixture_id)
        except CooldownActiveError:
            return False
        return True

    def _recalc(self, stored: _StoredSignal) -> RecalcDiff:
        """New odds (or cached numbers when the reserve is spent) and the verdict.

        The verdict uses the thresholds the signal was created/recommended with.
        """
        before = self._state.view(stored)
        if self._state.budget_remaining() >= RECALC_REQUESTS:
            self._state.spend(RECALC_REQUESTS)
            stored.odds = drifted_odds(stored.odds, stored.key, stored.version)
            stored.version += 1
            stored.data_as_of = None
        else:
            stored.data_as_of = self._state.refreshed_at()
        after = self._state.view(stored)
        valid = stored.thresholds.passes(replace(stored.quote, odds=stored.odds))
        return RecalcDiff(
            signal=after,
            before=_numbers(before),
            after=_numbers(after),
            verdict=RecalcVerdict.STILL_VALID if valid else RecalcVerdict.BELOW_THRESHOLD,
            recalculated_at=self._state.now(),
        )

    async def fixture_detail(self, fixture_id: EntityId) -> FixtureDetail:
        """See :meth:`AnalysisService.fixture_detail`."""
        return build_detail(self._state.fixture(fixture_id), self._state.now())


class FakeSignalService:
    """Fake :class:`~app.services.interfaces.SignalService`."""

    def __init__(self, state: FakeState) -> None:
        """Share ``state`` with the other fake services."""
        self._state = state

    async def get_signal(self, user_id: int, signal_id: SignalId) -> SignalView:
        """See :meth:`SignalService.get_signal`."""
        return self._state.view(self._state.signal(user_id, signal_id))

    async def get_signals(
        self, user_id: int, signal_ids: Sequence[SignalId]
    ) -> tuple[SignalView, ...]:
        """See :meth:`SignalService.get_signals`."""
        found = (self._state.signals.get(signal_id) for signal_id in signal_ids)
        return tuple(
            self._state.view(stored)
            for stored in found
            if stored is not None and stored.user_id == user_id
        )

    async def list_signals(
        self, user_id: int, dates: DateSpec, *, include_hidden: bool = False
    ) -> tuple[SignalView, ...]:
        """See :meth:`SignalService.list_signals`."""
        zone = self._state.settings_for(user_id).zone
        window = dates.resolve(self._state.local_today(zone))
        return tuple(
            self._state.view(stored)
            for stored in self._state.user_signals(user_id)
            if not stored.retro
            and (include_hidden or stored.state is not SignalState.HIDDEN)
            and window.contains(stored.fixture.kickoff, zone)
        )

    async def hide_signal(self, user_id: int, signal_id: SignalId) -> SignalView:
        """See :meth:`SignalService.hide_signal`."""
        stored = self._state.signal(user_id, signal_id)
        stored.state = SignalState.HIDDEN
        return self._state.view(stored)


def _check_search(text: str, limit: int) -> str:
    """Normalized search text; short text or a bad limit is a bot input error."""
    normalized = normalize_name(text)
    if len(normalized) < MIN_SEARCH_LENGTH:
        raise ValidationFailedError("text", f"type at least {MIN_SEARCH_LENGTH} characters")
    if not 1 <= limit <= MAX_SEARCH_RESULTS:
        raise ValidationFailedError("limit", f"must be within [1, {MAX_SEARCH_RESULTS}]")
    return normalized


def _name_score(query: str, name: str) -> float:
    """Substring matches first, then typo-tolerant similarity."""
    normalized = normalize_name(name)
    if query in normalized:
        return 2.0 - normalized.index(query) / (len(normalized) + 1)
    return SequenceMatcher(None, query, normalized).ratio()


class FakeCatalogService:
    """Fake :class:`~app.services.interfaces.CatalogService` (no Cyrillic transliteration)."""

    def __init__(self, state: FakeState) -> None:
        """Share ``state`` with the other fake services."""
        self._state = state

    async def list_sports(self) -> tuple[SportOption, ...]:
        """See :meth:`CatalogService.list_sports`."""
        return tuple(
            SportOption(
                sport=sport,
                available=sport in SPORT_MARKETS,
                experimental=sport in EXPERIMENTAL_SPORTS,
                supported_markets=SPORT_MARKETS.get(sport, frozenset()),
            )
            for sport in Sport
        )

    async def list_leagues(self, sport: Sport) -> tuple[LeagueOption, ...]:
        """See :meth:`CatalogService.list_leagues`."""
        leagues = [league for league in self._state.world.leagues if league.sport is sport]
        return tuple(sorted(leagues, key=lambda league: league.name))

    async def search_teams(
        self, text: str, *, sport: Sport | None = None, limit: int = 10
    ) -> tuple[TeamOption, ...]:
        """See :meth:`CatalogService.search_teams`."""
        query = _check_search(text, limit)
        scored = [
            (_name_score(query, team.name), team)
            for team in self._state.world.teams
            if sport is None or team.sport is sport
        ]
        matches = [item for item in scored if item[0] >= _FUZZY_THRESHOLD]
        matches.sort(key=lambda item: (-item[0], item[1].name))
        return tuple(team for _, team in matches[:limit])

    async def search_fixtures(self, text: str, *, limit: int = 10) -> tuple[FixtureOption, ...]:
        """See :meth:`CatalogService.search_fixtures`."""
        tokens = _check_search(text, limit).split()
        now = self._state.now()
        found = [
            fixture.option()
            for fixture in self._state.world.fixtures
            if not fixture.started(now)
            and all(
                token in normalize_name(f"{fixture.home.name} {fixture.away.name}")
                for token in tokens
            )
        ]
        return tuple(found[:limit])

    async def list_fixtures(self, user_id: int, request: FixtureListRequest) -> FixturePage:
        """See :meth:`CatalogService.list_fixtures`."""
        settings = self._state.settings_for(user_id)
        window = self._state.resolve(request.dates, settings)
        fixtures = [
            fixture.option()
            for fixture in self._state.fixtures_in(window, settings.zone, request.sports)
            if _matches_filters(fixture, request, settings.league_ids)
        ]
        page_count = -(-len(fixtures) // request.page_size)
        start = request.page * request.page_size
        return FixturePage(
            items=tuple(fixtures[start : start + request.page_size]),
            page=request.page,
            page_count=page_count,
            total=len(fixtures),
        )


def _matches_filters(
    fixture: FakeFixture, request: FixtureListRequest, default_leagues: frozenset[EntityId]
) -> bool:
    """Leagues and teams combine as "either"; no filter = the user's default leagues."""
    if not request.league_ids and not request.team_ids:
        return not default_leagues or fixture.league.league_id in default_leagues
    teams = {fixture.home.team_id, fixture.away.team_id}
    return fixture.league.league_id in request.league_ids or bool(teams & request.team_ids)


class FakeSettingsService:
    """Fake :class:`~app.services.interfaces.SettingsService`."""

    def __init__(self, state: FakeState) -> None:
        """Share ``state`` with the other fake services."""
        self._state = state

    async def get_settings(self, user_id: int) -> UserSettingsView:
        """See :meth:`SettingsService.get_settings`."""
        return self._state.settings_for(user_id)

    async def update_settings(self, user_id: int, settings: UserSettingsView) -> UserSettingsView:
        """See :meth:`SettingsService.update_settings`."""
        profile = infer_risk_profile(
            settings.min_probability, settings.min_ev, (settings.odds_min, settings.odds_max)
        )
        stored = replace(settings, risk_profile=profile)
        self._state.settings[user_id] = stored
        return stored

    async def apply_risk_profile(self, user_id: int, profile: RiskProfile) -> UserSettingsView:
        """See :meth:`SettingsService.apply_risk_profile`."""
        stored = self._state.settings_for(user_id).with_risk_profile(profile)
        self._state.settings[user_id] = stored
        return stored


class FakeTemplateService:
    """Fake :class:`~app.services.interfaces.TemplateService`."""

    def __init__(self, state: FakeState) -> None:
        """Share ``state`` with the other fake services."""
        self._state = state

    async def save_template(self, user_id: int, draft: TemplateDraft) -> TemplateView:
        """See :meth:`TemplateService.save_template`."""
        templates = self._state.templates.setdefault(user_id, {})
        name = draft.name.strip()
        existing = templates.get(name)
        template_id = existing.template_id if existing else next(self._state.next_id)
        view = TemplateView(
            template_id=template_id, name=name, query=draft.query, schedule=draft.schedule
        )
        templates[name] = view
        return view

    async def list_templates(self, user_id: int) -> tuple[TemplateView, ...]:
        """See :meth:`TemplateService.list_templates`."""
        templates = self._state.templates.get(user_id, {}).values()
        return tuple(sorted(templates, key=lambda view: view.name.casefold()))

    async def delete_template(self, user_id: int, template_id: TemplateId) -> None:
        """See :meth:`TemplateService.delete_template`."""
        templates = self._state.templates.get(user_id, {})
        for name, view in templates.items():
            if view.template_id == template_id:
                del templates[name]
                return
        raise NotFoundError("template", template_id)


class FakeWatchlistService:
    """Fake :class:`~app.services.interfaces.WatchlistService`."""

    def __init__(self, state: FakeState) -> None:
        """Share ``state`` with the other fake services."""
        self._state = state

    def _entity(self, target: WatchTarget) -> tuple[str, Sport]:
        world = self._state.world
        entities: dict[EntityId, LeagueOption | TeamOption] = (
            {team.team_id: team for team in world.teams}
            if target.kind is WatchKind.TEAM
            else {league.league_id: league for league in world.leagues}
        )
        found = entities.get(target.entity_id)
        if found is None:
            raise NotFoundError(target.kind.value, target.entity_id)
        return found.name, found.sport

    async def list_watchlist(self, user_id: int) -> tuple[WatchItem, ...]:
        """See :meth:`WatchlistService.list_watchlist`."""
        items = self._state.watchlist.get(user_id, {}).values()
        return tuple(sorted(items, key=lambda item: (item.name, item.target.kind)))

    async def add_watch(self, user_id: int, target: WatchTarget) -> WatchItem:
        """See :meth:`WatchlistService.add_watch`."""
        items = self._state.watchlist.setdefault(user_id, {})
        if target not in items:
            name, sport = self._entity(target)
            items[target] = WatchItem(target=target, name=name, sport=sport)
        return items[target]

    async def remove_watch(self, user_id: int, target: WatchTarget) -> bool:
        """See :meth:`WatchlistService.remove_watch`."""
        return self._state.watchlist.get(user_id, {}).pop(target, None) is not None

    async def set_notifications(
        self, user_id: int, target: WatchTarget, notifications: frozenset[WatchNotification]
    ) -> WatchItem:
        """See :meth:`WatchlistService.set_notifications`."""
        items = self._state.watchlist.get(user_id, {})
        if target not in items:
            raise NotFoundError("watch", target.entity_id)
        items[target] = replace(items[target], notifications=notifications)
        return items[target]


class FakeBetService:
    """Fake :class:`~app.services.interfaces.BetService`."""

    def __init__(self, state: FakeState) -> None:
        """Share ``state`` with the other fake services."""
        self._state = state

    async def place_bet(self, user_id: int, draft: BetDraft) -> BetView:
        """See :meth:`BetService.place_bet`."""
        stored = self._state.signal(user_id, draft.signal_id)
        if stored.retro:
            raise ValidationFailedError("signal_id", "a retro signal cannot be bet on")
        if draft.client_token is not None:
            known = self._state.bet_tokens.get((user_id, draft.client_token))
            if known is not None:
                return self.view(self._state.bets[known])
        stake = draft.stake or self._default_stake(user_id, stored)
        bet = _StoredBet(
            bet_id=next(self._state.next_id),
            user_id=user_id,
            signal_id=stored.signal_id,
            odds_taken=draft.odds_taken or stored.odds,
            stake=stake,
            placed_at=self._state.now(),
        )
        self._state.bets[bet.bet_id] = bet
        if draft.client_token is not None:
            self._state.bet_tokens[(user_id, draft.client_token)] = bet.bet_id
        return self.view(bet)

    def _default_stake(self, user_id: int, stored: _StoredSignal) -> Decimal:
        unit = self._state.settings_for(user_id).unit
        stake_units = self._state.view(stored).stake_units
        if unit is None or stake_units == 0:
            raise ValidationFailedError("stake", "enter the stake: no unit set or no stake advised")
        return unit * stake_units

    def view(self, bet: _StoredBet) -> BetView:
        """Bet as of now, settled from its signal's result."""
        stored = self._state.signals[bet.signal_id]
        outcome = self._state.result_of(stored)
        return BetView(
            bet_id=bet.bet_id,
            signal_id=bet.signal_id,
            fixture_label=f"{stored.fixture.home.name} — {stored.fixture.away.name}",
            market=stored.quote.market,
            selection=stored.quote.selection,
            odds_taken=bet.odds_taken,
            stake=bet.stake,
            placed_at=bet.placed_at,
            outcome=outcome,
            profit=self._profit(bet, stored, outcome),
        )

    def _profit(
        self, bet: _StoredBet, stored: _StoredSignal, outcome: ResultMark | None
    ) -> Decimal | None:
        """Bet profit; a cash-out scales the signal's cash-out units to the bet stake."""
        if outcome is ResultMark.CASHOUT:
            units = self._state.view(stored).stake_units or Decimal(1)
            return bet.stake * (stored.cashout_units or Decimal(0)) / units
        view = replace(self._state.view(stored), odds=bet.odds_taken, stake_units=bet.stake)
        return profit_units(view, None)

    def user_bets(self, user_id: int) -> list[BetView]:
        """All bets of the user, most recent first."""
        bets = [bet for bet in self._state.bets.values() if bet.user_id == user_id]
        bets.sort(key=lambda bet: (bet.placed_at, bet.bet_id), reverse=True)
        return [self.view(bet) for bet in bets]

    async def list_bets(
        self, user_id: int, *, open_only: bool = False, limit: int = 20
    ) -> tuple[BetView, ...]:
        """See :meth:`BetService.list_bets`."""
        if not 1 <= limit <= _MAX_BET_LIST:
            raise ValidationFailedError("limit", f"must be within [1, {_MAX_BET_LIST}]")
        bets = [bet for bet in self.user_bets(user_id) if not open_only or bet.outcome is None]
        return tuple(bets[:limit])

    async def bank_summary(self, user_id: int) -> BankView:
        """See :meth:`BetService.bank_summary`."""
        settings = self._state.settings_for(user_id)
        today = self._state.local_today(settings.zone)
        by_day = self._profit_by_day(user_id, settings.zone)
        week_start = today - timedelta(days=today.weekday())
        loss_today = _loss(profit for day, profit in by_day.items() if day == today)
        loss_week = _loss(profit for day, profit in by_day.items() if day >= week_start)
        month = (today.year, today.month)
        total = sum(by_day.values(), Decimal(0))
        return BankView(
            balance=None if settings.bank is None else settings.bank + total,
            unit=settings.unit,
            month_profit=sum(
                (profit for day, profit in by_day.items() if (day.year, day.month) == month),
                Decimal(0),
            ),
            open_bets=sum(bet.outcome is None for bet in self.user_bets(user_id)),
            history=_bank_history(settings.bank, by_day),
            loss_today=loss_today,
            loss_this_week=loss_week,
            loss_limit_reached=_limit_reached(settings, loss_today, loss_week),
        )

    def _profit_by_day(self, user_id: int, zone: ZoneInfo) -> dict[date, Decimal]:
        """Settled bet profit per local match day."""
        by_day: dict[date, Decimal] = {}
        for bet in self._state.bets.values():
            if bet.user_id != user_id:
                continue
            profit = self.view(bet).profit
            if profit is not None:
                day = self._state.signals[bet.signal_id].fixture.kickoff.astimezone(zone).date()
                by_day[day] = by_day.get(day, Decimal(0)) + profit
        return by_day


def _loss(profits: Iterable[Decimal]) -> Decimal:
    """Net loss as a positive amount (zero when in profit)."""
    return max(-sum(profits, Decimal(0)), Decimal(0))


def _bank_history(bank: Decimal | None, by_day: dict[date, Decimal]) -> tuple[BankPoint, ...]:
    if bank is None:
        return ()
    points, balance = [], bank
    for day in sorted(by_day):
        balance += by_day[day]
        points.append(BankPoint(day, balance))
    return tuple(points)


def _limit_reached(settings: UserSettingsView, loss_today: Decimal, loss_week: Decimal) -> bool:
    daily, weekly = settings.daily_loss_limit, settings.weekly_loss_limit
    return (daily is not None and loss_today >= daily) or (
        weekly is not None and loss_week >= weekly
    )


class FakeResultService:
    """Fake :class:`~app.services.interfaces.ResultService`."""

    def __init__(self, state: FakeState) -> None:
        """Share ``state`` with the other fake services."""
        self._state = state

    async def pending_results(self, user_id: int) -> tuple[SignalView, ...]:
        """See :meth:`ResultService.pending_results`."""
        now = self._state.now()
        return tuple(
            self._state.view(stored)
            for stored in self._state.user_signals(user_id)
            if stored.counts
            and stored.fixture.finished(now)
            and self._state.result_of(stored) is None
        )

    async def mark_result(self, user_id: int, request: ResultMarkRequest) -> SignalView:
        """See :meth:`ResultService.mark_result`."""
        stored = self._state.signal(user_id, request.signal_id)
        if stored.retro:
            raise ValidationFailedError("signal_id", "a retro signal has no result to mark")
        if not stored.fixture.started(self._state.now()):
            raise ValidationFailedError("signal_id", "the match has not started yet")
        stored.manual_mark = request.mark
        stored.cashout_units = request.cashout_profit_units
        return self._state.view(stored)

    async def add_note(self, user_id: int, signal_id: SignalId, text: str) -> None:
        """See :meth:`ResultService.add_note`."""
        stored = self._state.signal(user_id, signal_id)
        note = text.strip()
        if not note or len(note) > MAX_NOTE_LENGTH:
            raise ValidationFailedError("text", f"must be 1..{MAX_NOTE_LENGTH} characters")
        stored.notes.append(note)

    async def day_summary(self, user_id: int, day: date) -> DaySummary:
        """See :meth:`ResultService.day_summary`."""
        zone = self._state.settings_for(user_id).zone
        rows = [
            row
            for row in self._state.outcomes(user_id)
            if row.view.kickoff.astimezone(zone).date() == day
        ]
        return day_summary(rows, day)


class FakeReportService:
    """Fake :class:`~app.services.interfaces.ReportService`."""

    def __init__(self, state: FakeState, bets: FakeBetService) -> None:
        """Share ``state``; bets give the ROI of the user's own stakes."""
        self._state = state
        self._bets = bets

    async def report(self, user_id: int, request: ReportRequest) -> ReportView:
        """See :meth:`ReportService.report`."""
        zone = self._state.settings_for(user_id).zone
        date_from, date_to = request.resolve(self._state.local_today(zone))

        def in_period(kickoff: datetime) -> bool:
            day = kickoff.astimezone(zone).date()
            return (date_from is None or day >= date_from) and day <= date_to

        rows = [row for row in self._state.outcomes(user_id) if in_period(row.view.kickoff)]
        period_signals = {row.view.signal_id for row in rows}
        bets = [
            bet
            for bet in self._bets.user_bets(user_id)
            if bet.profit is not None and bet.signal_id in period_signals
        ]
        bet_totals = (
            sum((bet.profit or Decimal(0) for bet in bets), Decimal(0)),
            sum((bet.stake for bet in bets), Decimal(0)),
        )
        return build_report((date_from, date_to), request.group_by, rows, bet_totals)

    async def export(self, user_id: int) -> ExportFile:
        """See :meth:`ReportService.export`."""
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(_EXPORT_HEADER)
        for row in self._state.outcomes(user_id):
            writer.writerow(_export_row(row))
        return ExportFile(filename="signals.csv", content=buffer.getvalue().encode("utf-8"))


_EXPORT_HEADER: Final = (
    "signal_id",
    "kickoff_utc",
    "sport",
    "league",
    "home",
    "away",
    "market",
    "line",
    "selection",
    "odds",
    "probability",
    "ev",
    "confidence",
    "stake_units",
    "recommended_at",
    "hidden",
    "result",
    "profit_units",
)


def _export_row(row: SignalOutcome) -> tuple[object, ...]:
    view = row.view
    return (
        view.signal_id,
        view.kickoff.isoformat(),
        view.sport.value,
        view.league,
        view.home_team,
        view.away_team,
        view.market.type.value,
        "" if view.market.line is None else view.market.line,
        view.selection.value,
        view.odds,
        round(view.probability, 4),
        round(view.ev, 4),
        view.confidence.value,
        view.stake_units,
        "" if view.recommended_at is None else view.recommended_at.isoformat(),
        view.state is SignalState.HIDDEN,
        "" if view.result is None else view.result.value,
        "" if row.profit_units is None else row.profit_units,
    )


class FakeSystemStatusService:
    """Fake :class:`~app.services.interfaces.SystemStatusService`."""

    def __init__(self, state: FakeState) -> None:
        """Share ``state`` with the other fake services."""
        self._state = state

    async def system_status(self) -> SystemStatus:
        """See :meth:`SystemStatusService.system_status`."""
        refreshed = self._state.refreshed_at()
        manual_used = FAKE_MANUAL_RESERVE - self._state.budget_remaining()
        quota = ApiQuota(
            provider=PROVIDER,
            sport=Sport.FOOTBALL,
            used=FAKE_AUTOMATIC_USAGE + manual_used,
            limit=FAKE_DAILY_LIMIT,
            reserved_manual=FAKE_MANUAL_RESERVE,
        )
        jobs = (
            JobStatus(name="refresh", last_run_at=refreshed, ok=True),
            JobStatus(name="daily", last_run_at=refreshed + timedelta(minutes=30), ok=True),
            JobStatus(name="settle", last_run_at=refreshed - timedelta(minutes=30), ok=True),
        )
        return SystemStatus(quotas=(quota,), last_refresh_at=refreshed, jobs=jobs, recent_errors=())


@dataclass(frozen=True, slots=True)
class SentNotification:
    """One call recorded by :class:`FakeNotifier`."""

    method: str
    user_id: int | None
    payload: object
    key: DeliveryKey | None
    message: MessageRef | None


class FakeNotifier:
    """Recording :class:`~app.services.interfaces.Notifier` for bot and pipeline tests.

    Every call is appended to :attr:`sent` and answered with a fresh :class:`MessageRef`
    (chat id = user id), so a test can assert what was announced, to whom and how often.
    """

    def __init__(self) -> None:
        """Start with nothing sent."""
        self.sent: list[SentNotification] = []
        self._ids = itertools.count(1)

    def _record(
        self, method: str, user_id: int | None, payload: object, key: DeliveryKey | None
    ) -> MessageRef | None:
        message = None if user_id is None else MessageRef(user_id, next(self._ids))
        self.sent.append(SentNotification(method, user_id, payload, key, message))
        return message

    async def send_daily_digest(
        self, user_id: int, digest: DailyDigest, *, key: DeliveryKey
    ) -> MessageRef | None:
        """See :meth:`Notifier.send_daily_digest`."""
        return self._record("daily_digest", user_id, digest, key)

    async def send_signal_update(
        self,
        user_id: int,
        update: SignalUpdate,
        *,
        key: DeliveryKey,
        reply_to: MessageRef | None = None,  # noqa: ARG002 - part of the protocol
    ) -> MessageRef | None:
        """See :meth:`Notifier.send_signal_update`."""
        return self._record("signal_update", user_id, update, key)

    async def send_signal_cancelled(
        self,
        user_id: int,
        cancelled: SignalCancelled,
        *,
        key: DeliveryKey,
        reply_to: MessageRef | None = None,  # noqa: ARG002 - part of the protocol
    ) -> MessageRef | None:
        """See :meth:`Notifier.send_signal_cancelled`."""
        return self._record("signal_cancelled", user_id, cancelled, key)

    async def send_watchlist_event(
        self, user_id: int, event: WatchlistEvent, *, key: DeliveryKey
    ) -> MessageRef | None:
        """See :meth:`Notifier.send_watchlist_event`."""
        return self._record("watchlist_event", user_id, event, key)

    async def send_result_settled(
        self, user_id: int, notice: SettledNotice, *, key: DeliveryKey
    ) -> MessageRef | None:
        """See :meth:`Notifier.send_result_settled`."""
        return self._record("result_settled", user_id, notice, key)

    async def alert_admin(self, alert: AdminAlert) -> None:
        """See :meth:`Notifier.alert_admin`."""
        self._record("alert_admin", None, alert, None)


def create_fake_services(*, clock: Clock = system_clock) -> Services:
    """All fake services over one shared in-memory state."""
    state = FakeState(clock)
    bets = FakeBetService(state)
    return Services(
        analysis=FakeAnalysisService(state),
        signals=FakeSignalService(state),
        catalog=FakeCatalogService(state),
        settings=FakeSettingsService(state),
        templates=FakeTemplateService(state),
        watchlist=FakeWatchlistService(state),
        bets=bets,
        results=FakeResultService(state),
        reports=FakeReportService(state, bets),
        status=FakeSystemStatusService(state),
    )
