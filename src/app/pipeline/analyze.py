"""The one analysis path (F4.2): daily, date-range, custom, template and watchlist runs.

``AnalysisEngine.run`` is the whole chain for one user:

1. load the fixtures of the window and keep those in the request's scope;
2. fetch what is missing from the provider within the budget (``plan`` estimates it first);
3. fit the leagues (cached), price every fixture (:mod:`app.pipeline.valuation`) and apply the
   thresholds, caps and watchlist rules (:mod:`app.pipeline.selection`);
4. have the LLM hook review the recommendations (optional) and store the signals
   (:mod:`app.pipeline.persist`).

Only fixtures that have not kicked off give real signals; finished fixtures give ``retro``
signals (what the model would have said, never a recommendation) unless the request asks to
``skip_retro`` (the daily digest); in-play and postponed/cancelled fixtures are skipped. One
bad fixture is logged and counted, never fatal. Fixtures the user's bookmaker has no (fresh)
price for are counted too (``skipped_no_price`` / ``skipped_stale_price``), and a run whose
provider data could not be refreshed says so (``data_stale``): "0 signals" is then not the
same as "nothing to bet on".
"""

from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Final

from app.clients.cost import FetchPlan, estimate_requests
from app.db.enums import QueryScope
from app.db.repo import FixtureRecord, list_fixtures
from app.domain import ExternalRef, FixtureStatus, OddsKind, OddsSnapshot, Sport
from app.logging import get_logger
from app.pipeline.context import PipelineContext
from app.pipeline.errors import BudgetExhaustedError, PipelineError
from app.pipeline.ingest import (
    ODDS_COVERAGE,
    ingest_fixtures,
    ingest_odds,
    ingest_results,
    is_covered,
    utc_days,
)
from app.pipeline.modeling import FittedLeague
from app.pipeline.odds import current_quotes, load_decision_snapshots
from app.pipeline.persist import store_picks
from app.pipeline.provider import MANUAL, QuotaSnapshot
from app.pipeline.queries import league_refs, team_refs
from app.pipeline.selection import Pick, assemble, picks_for_fixture
from app.pipeline.types import AnalysisOutcome, AnalysisRequest, DataPlan
from app.pipeline.valuation import SKIP_NO_PRICE, SKIP_STALE_PRICE, ValuationInput, value_fixture
from app.sports.football_leagues import seasons_between

logger = get_logger(__name__)

type ProgressSink = Callable[[str, int, int], Awaitable[None]]
"""``(stage, done, total)`` with stage names equal to the contract's ``AnalysisStage`` values."""

STAGE_COLLECTING: Final = "collecting"
STAGE_ANALYSING: Final = "analysing"
STAGE_REVIEWING: Final = "reviewing"
STAGE_FINISHED: Final = "finished"
MIN_FIXTURES_FOR_LEAGUE_ODDS: Final = 2
_NOT_PLAYABLE: Final = frozenset(
    {FixtureStatus.POSTPONED, FixtureStatus.CANCELLED, FixtureStatus.ABANDONED}
)


@dataclass(frozen=True, slots=True)
class ScopeRefs:
    """The request's scope resolved from database ids to canonical references."""

    leagues: frozenset[ExternalRef]
    default_leagues: frozenset[ExternalRef]
    watch_leagues: frozenset[ExternalRef]
    teams: frozenset[ExternalRef]
    watch_teams: frozenset[ExternalRef]


@dataclass(slots=True)
class Tally:
    """Per-run counters of fixtures that gave no pick for a price reason."""

    failed: int = 0
    no_price: int = 0
    stale_price: int = 0


@dataclass(frozen=True, slots=True)
class WindowItem:
    """A fixture of the window that is in scope; ``watched`` = followed by the user."""

    record: FixtureRecord
    watched: bool


@dataclass(frozen=True, slots=True)
class DataNeeds:
    """What a window still needs from the provider."""

    sport: Sport
    fixture_days: tuple[date, ...] = ()
    odds_refs: tuple[ExternalRef, ...] = ()
    odds_days: tuple[date, ...] = ()
    odds_league_days: int = 0
    odds_singles: int = 0
    result_refs: tuple[ExternalRef, ...] = ()
    fixture_requests: int = 0

    @property
    def requests(self) -> int:
        """Provider requests the fetch would spend (an upper bound)."""
        return estimate_requests(
            FetchPlan(
                days=self.fixture_requests,
                odds_league_days=self.odds_league_days,
                odds_fixtures=self.odds_singles,
                fixtures_by_id=len(self.result_refs),
            )
        )


def classify(record: FixtureRecord, now: datetime) -> str | None:
    """``"upcoming"``, ``"retro"`` (finished with a score) or ``None`` (not analysable)."""
    fixture = record.fixture
    if fixture.status in _NOT_PLAYABLE:
        return None
    if fixture.status is FixtureStatus.SCHEDULED and fixture.kickoff > now:
        return "upcoming"
    if fixture.status is FixtureStatus.FINISHED and record.result is not None:
        return "retro"
    return None


def _analysable(record: FixtureRecord, request: AnalysisRequest, now: datetime) -> bool:
    kind = classify(record, now)
    return kind is not None and not (request.skip_retro and kind == "retro")


def _real_count(items: Sequence[WindowItem], now: datetime) -> int:
    """Fixtures that can still give a recommendation; a pure look-back counts them all."""
    upcoming = sum(1 for item in items if classify(item.record, now) == "upcoming")
    return upcoming or len(items)


def as_of_for(record: FixtureRecord, kind: str, now: datetime) -> date:
    """Model cut-off day: tomorrow for upcoming fixtures, the match day for retro ones."""
    if kind == "retro":
        return record.fixture.kickoff.date()
    return now.astimezone(UTC).date() + timedelta(days=1)


class AnalysisEngine:
    """Runs analyses for one :class:`PipelineContext`."""

    def __init__(self, ctx: PipelineContext) -> None:
        """Use ``ctx`` for data, providers, models and the clock."""
        self._ctx = ctx

    # ------------------------------------------------------------------ planning
    async def plan(self, request: AnalysisRequest) -> DataPlan:
        """Fixtures in scope and the provider requests a run would spend (nothing is spent)."""
        now = self._ctx.clock()
        items, _ = await self._collect(request)
        needs = await self._needs(request, items, now)
        return DataPlan(
            fixtures_found=sum(1 for item in items if classify(item.record, now) is not None),
            api_requests=needs.requests,
            budget_remaining=(await self._quota(request)).available(request.purpose),
        )

    async def _quota(self, request: AnalysisRequest) -> QuotaSnapshot:
        provider = self._ctx.providers.get(Sport.FOOTBALL)
        if provider is None:
            return QuotaSnapshot(
                provider="none",
                sport=None,
                used=0,
                limit=0,
                manual_used=0,
                remaining=0,
                automatic_available=0,
            )
        _ = request
        return await provider.quota()

    # ------------------------------------------------------------------ running
    async def run(
        self, request: AnalysisRequest, *, progress: ProgressSink | None = None
    ) -> AnalysisOutcome:
        """The whole chain (see the module docstring).

        Raises:
            BudgetExhaustedError: a manual request needs more than the quota allows.
            ProviderUnavailableError: a manual request could not fetch what it needs.
        """
        ctx = self._ctx
        used_before = (await self._quota(request)).used
        items, _ = await self._collect(request)
        now = ctx.clock()
        data_stale = False
        if request.fetch:
            data_stale = not await self._ensure(request, await self._needs(request, items, now))
            items, _ = await self._collect(request)
        used = max((await self._quota(request)).used - used_before, 0)
        analysable = [item for item in items if _analysable(item.record, request, now)]
        await _emit(progress, STAGE_COLLECTING, 0, len(analysable))
        by_fixture, tally = await self._value(request, analysable, now, progress)
        assembly = assemble(by_fixture, request)
        await _emit(progress, STAGE_REVIEWING, len(analysable), len(analysable))
        stored = await store_picks(ctx, request, assembly.picks, matched=assembly.matched)
        await _emit(progress, STAGE_FINISHED, len(analysable), len(analysable))
        return AnalysisOutcome(
            cards=stored.cards,
            fixtures_analyzed=_real_count(analysable, now),
            matched=stored.matched,
            requested=request.max_signals,
            api_requests_used=used,
            failed_fixtures=tally.failed,
            skipped_no_price=tally.no_price,
            skipped_stale_price=tally.stale_price,
            data_stale=data_stale,
        )

    async def _value(
        self,
        request: AnalysisRequest,
        items: Sequence[WindowItem],
        now: datetime,
        progress: ProgressSink | None,
    ) -> tuple[list[list[Pick]], Tally]:
        ctx = self._ctx
        async with ctx.sessions() as session:
            refs = [item.record.fixture.ref for item in items]
            snapshots = await load_decision_snapshots(session, refs)
        fitted: dict[tuple[ExternalRef, date], FittedLeague] = {}
        by_fixture: list[list[Pick]] = []
        tally = Tally()
        for done, item in enumerate(items, start=1):
            try:
                picks = await self._value_one(request, item, snapshots, fitted, (now, tally))
            except Exception as error:  # one bad fixture must not fail the other 29
                tally.failed += 1
                logger.warning(
                    "fixture_failed", fixture=str(item.record.fixture.ref), error=repr(error)
                )
                picks = []
            by_fixture.append(picks)
            await _emit(progress, STAGE_ANALYSING, done, len(items))
        return by_fixture, tally

    async def _value_one(
        self,
        request: AnalysisRequest,
        item: WindowItem,
        snapshots: dict[ExternalRef, list[OddsSnapshot]],
        fitted: dict[tuple[ExternalRef, date], FittedLeague],
        run: tuple[datetime, Tally],
    ) -> list[Pick]:
        ctx = self._ctx
        now, tally = run
        record = item.record
        fixture = record.fixture
        kind = classify(record, now) or "upcoming"
        as_of = as_of_for(record, kind, now)
        league = ctx.leagues.canonical(fixture.league.ref)
        league_fit = fitted.get((league, as_of))
        if league_fit is None:
            league_fit = await ctx.models.get(league, as_of)
            fitted[(league, as_of)] = league_fit
        quotes = current_quotes(snapshots.get(fixture.ref, []), fixture.ref, fixture.kickoff)
        valuation = value_fixture(
            ValuationInput(
                fixture=fixture,
                quotes=quotes,
                fitted=league_fit,
                now=now,
                markets=request.markets,
                allow_model_only=request.include_no_value,
                require_fresh=kind == "upcoming",
            ),
            ctx.config,
        )
        if valuation.skip is not None:
            logger.debug("fixture_skipped", fixture=str(fixture.ref), reason=valuation.skip)
            tally.no_price += valuation.skip == SKIP_NO_PRICE
            tally.stale_price += valuation.skip == SKIP_STALE_PRICE
        return picks_for_fixture(
            record,
            valuation.candidates,
            request,
            watched=request.scope is QueryScope.ALL and item.watched,
            retro=kind == "retro",
        )

    # ------------------------------------------------------------------ scope
    async def _collect(self, request: AnalysisRequest) -> tuple[list[WindowItem], ScopeRefs]:
        ctx = self._ctx
        async with ctx.sessions() as session:
            page = await list_fixtures(session, start=request.start, end=request.end)
            leagues = await league_refs(
                session,
                request.league_ids | request.default_league_ids | request.watch_league_ids,
            )
            teams = await team_refs(session, request.team_ids | request.watch_team_ids)
        canonical = ctx.leagues.canonical

        def canon(ids: frozenset[int]) -> frozenset[ExternalRef]:
            return frozenset(canonical(leagues[i]) for i in ids if i in leagues)

        def team_set(ids: frozenset[int]) -> frozenset[ExternalRef]:
            return frozenset(teams[i] for i in ids if i in teams)

        scope = ScopeRefs(
            leagues=canon(request.league_ids),
            default_leagues=canon(request.default_league_ids),
            watch_leagues=canon(request.watch_league_ids),
            teams=team_set(request.team_ids),
            watch_teams=team_set(request.watch_team_ids),
        )
        items = []
        for record in page.items:
            if record.fixture.sport not in request.sports:
                continue
            watched = self._watched(record, scope)
            if self._included(record, request, scope, watched=watched):
                items.append(WindowItem(record, watched))
        return items, scope

    def _watched(self, record: FixtureRecord, scope: ScopeRefs) -> bool:
        fixture = record.fixture
        return (
            self._ctx.leagues.canonical(fixture.league.ref) in scope.watch_leagues
            or fixture.home.ref in scope.watch_teams
            or fixture.away.ref in scope.watch_teams
        )

    def _included(
        self, record: FixtureRecord, request: AnalysisRequest, scope: ScopeRefs, *, watched: bool
    ) -> bool:
        fixture = record.fixture
        league = self._ctx.leagues.canonical(fixture.league.ref)
        match request.scope:
            case QueryScope.ALL:
                in_leagues = not scope.default_leagues or league in scope.default_leagues
                return in_leagues or watched
            case QueryScope.LEAGUES:
                return league in scope.leagues
            case QueryScope.TEAMS:
                return fixture.home.ref in scope.teams or fixture.away.ref in scope.teams
            case QueryScope.FIXTURES:
                return record.id in request.fixture_ids

    # ------------------------------------------------------------------ provider data
    async def _needs(
        self, request: AnalysisRequest, items: Sequence[WindowItem], now: datetime
    ) -> DataNeeds:
        ctx = self._ctx
        provider = ctx.providers.get(Sport.FOOTBALL)
        if provider is None or not request.fetch or Sport.FOOTBALL not in request.sports:
            return DataNeeds(sport=Sport.FOOTBALL)
        source = provider.source
        days = [day for day in utc_days(request.start, request.end) if day >= now.date()]
        uncovered = [
            day
            for day in days
            if not await is_covered(
                ctx, "cover.fixtures", day, ttl=ctx.config.fixtures_ttl, sport=provider.sport
            )
        ]
        seasons = len(seasons_between(request.start.date(), request.end.date()))
        leagues = len(ctx.leagues.canonical_refs())
        fixture_requests = min(len(uncovered), leagues * seasons)
        odds_refs, odds_days = await self._odds_needs(items, now, source)
        groups: defaultdict[tuple[ExternalRef, date], int] = defaultdict(int)
        for _, league, day in odds_refs:
            groups[(league, day)] += 1
        # Days not fetched yet have no stored fixtures to name: assume one odds request per
        # league and day (an upper bound).
        league_days = len(uncovered) * leagues + sum(
            1 for count in groups.values() if count >= MIN_FIXTURES_FOR_LEAGUE_ODDS
        )
        singles = sum(count for count in groups.values() if count < MIN_FIXTURES_FOR_LEAGUE_ODDS)
        result_refs = tuple(
            item.record.fixture.ref
            for item in items
            if item.record.fixture.ref.source == source
            and item.record.fixture.status in {FixtureStatus.SCHEDULED, FixtureStatus.LIVE}
            and item.record.fixture.kickoff + ctx.config.match_duration <= now
        )
        return DataNeeds(
            sport=provider.sport,
            fixture_days=tuple(uncovered),
            odds_refs=tuple(ref for ref, _, _ in odds_refs),
            odds_days=tuple(odds_days),
            odds_league_days=league_days,
            odds_singles=singles,
            result_refs=result_refs,
            fixture_requests=fixture_requests,
        )

    async def _odds_needs(
        self, items: Sequence[WindowItem], now: datetime, source: str
    ) -> tuple[list[tuple[ExternalRef, ExternalRef, date]], list[date]]:
        ctx = self._ctx
        refs: list[tuple[ExternalRef, ExternalRef, date]] = []
        days: set[date] = set()
        fresh: dict[date, bool] = {}
        for item in items:
            fixture = item.record.fixture
            if fixture.ref.source != source or classify(item.record, now) != "upcoming":
                continue
            day = fixture.kickoff.date()
            if day not in fresh:
                fresh[day] = await is_covered(ctx, ODDS_COVERAGE, day, ttl=ctx.config.odds_ttl)
            if not fresh[day]:
                refs.append((fixture.ref, fixture.league.ref, day))
                days.add(day)
        return refs, sorted(days)

    async def _ensure(self, request: AnalysisRequest, needs: DataNeeds) -> bool:
        """Fetch what is missing; ``False`` when an automatic run had to go without it."""
        ctx = self._ctx
        if needs.requests == 0:
            return True
        quota = await ctx.providers[needs.sport].quota()
        available = quota.available(request.purpose)
        if needs.requests > available:
            if request.purpose is MANUAL:
                raise BudgetExhaustedError(needs.requests, available)
            logger.warning("analysis_data_skipped", needed=needs.requests, available=available)
            return False
        try:
            await self._fetch(request, needs)
        except PipelineError:
            if request.purpose is MANUAL:
                raise
            logger.exception("analysis_fetch_failed")
            return False
        return True

    async def _fetch(self, request: AnalysisRequest, needs: DataNeeds) -> None:
        ctx = self._ctx
        sport, purpose = needs.sport, request.purpose
        await ingest_results(ctx, sport, needs.result_refs, purpose=purpose)
        refs, days = list(needs.odds_refs), needs.odds_days
        if needs.fixture_days:
            await ingest_fixtures(
                ctx, sport, min(needs.fixture_days), max(needs.fixture_days), purpose=purpose
            )
            # Fixtures just stored may need prices that the plan could not name yet.
            items, _ = await self._collect(request)
            fresh, fresh_days = await self._odds_needs(
                items, ctx.clock(), ctx.providers[sport].source
            )
            refs, days = [ref for ref, _, _ in fresh], tuple(fresh_days)
        kind = OddsKind.RECALC if purpose is MANUAL else OddsKind.MORNING
        await ingest_odds(ctx, sport, refs, kind=kind, purpose=purpose, days=days)


async def _emit(progress: ProgressSink | None, stage: str, done: int, total: int) -> None:
    if progress is not None:
        await progress(stage, done, total)
