"""The one analysis path (F4.2): daily, date-range, custom, template and watchlist runs.

``AnalysisEngine.run`` is the whole chain for one user:

1. check that the database was refreshed recently (a manual request is refused otherwise);
2. load the fixtures of the window from the database and keep those in the request's scope;
3. fit the leagues (cached), price every fixture (:mod:`app.pipeline.valuation`) and apply the
   thresholds, caps and watchlist rules (:mod:`app.pipeline.selection`);
4. have the LLM hook review the recommendations (optional) and store the signals
   (:mod:`app.pipeline.persist`).

Only fixtures that have not kicked off give real signals; finished fixtures give ``retro``
signals (what the model would have said, never a recommendation) unless the request asks to
``skip_retro`` (the daily digest); in-play and postponed/cancelled fixtures are skipped. One
bad fixture is logged and counted, never fatal. Fixtures the user's bookmaker has no (fresh)
price for are counted too (``skipped_no_price`` / ``skipped_stale_price``), and a run on a
database that was not refreshed lately says so (``data_stale``): "0 signals" is then not the
same as "nothing to bet on".

The engine never calls the data provider: fixtures, prices and results come from the daily
refresh (:mod:`app.pipeline.refresh`) and the administrator's manual refresh (ADR 0021).
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Final

from app.db.enums import QueryScope
from app.db.repo import FixtureRecord, league_refs, list_fixtures, team_refs
from app.domain import ExternalRef, FixtureStatus, OddsSnapshot, Sport
from app.logging import get_logger
from app.pipeline.context import PipelineContext
from app.pipeline.freshness import freshness, require_fresh
from app.pipeline.modeling import FittedLeague
from app.pipeline.odds import current_quotes, load_decision_snapshots
from app.pipeline.overlay import EMPTY_OVERLAY, LearningOverlay, load_overlay
from app.pipeline.persist import store_picks
from app.pipeline.provider import MANUAL
from app.pipeline.selection import Pick, assemble, picks_for_fixture
from app.pipeline.types import AnalysisOutcome, AnalysisRequest, DataPlan
from app.pipeline.valuation import SKIP_NO_PRICE, SKIP_STALE_PRICE, ValuationInput, value_fixture

logger = get_logger(__name__)

type ProgressSink = Callable[[str, int, int], Awaitable[None]]
"""``(stage, done, total)`` with stage names equal to the contract's ``AnalysisStage`` values."""

STAGE_COLLECTING: Final = "collecting"
STAGE_ANALYSING: Final = "analysing"
STAGE_REVIEWING: Final = "reviewing"
STAGE_FINISHED: Final = "finished"
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
        """Fixtures in scope; a run spends no provider requests (nothing is fetched)."""
        now = self._ctx.clock()
        items, _ = await self._collect(request)
        current = await freshness(self._ctx, request.end)
        return DataPlan(
            fixtures_found=sum(1 for item in items if classify(item.record, now) is not None),
            api_requests=0,
            budget_remaining=await self._budget(request),
            data_stale=current.stale,
            last_refresh_at=current.last_refresh_at,
        )

    async def _budget(self, request: AnalysisRequest) -> int:
        """Provider requests the request's purpose may still spend today (display only)."""
        provider = self._ctx.providers.get(Sport.FOOTBALL)
        return 0 if provider is None else (await provider.quota()).available(request.purpose)

    # ------------------------------------------------------------------ running
    async def run(
        self, request: AnalysisRequest, *, progress: ProgressSink | None = None
    ) -> AnalysisOutcome:
        """The whole chain (see the module docstring).

        Raises:
            StaleDataError: a manual request reaches into the future and the database was not
                refreshed lately.
        """
        ctx = self._ctx
        if request.purpose is MANUAL:
            current = await require_fresh(ctx, request.end)
        else:
            current = await freshness(ctx, request.end)
        items, _ = await self._collect(request)
        now = ctx.clock()
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
            api_requests_used=0,
            failed_fixtures=tally.failed,
            skipped_no_price=tally.no_price,
            skipped_stale_price=tally.stale_price,
            data_stale=current.stale,
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
        # one read per run (calibration and segment rules); a failure means the raw model
        overlay = await load_overlay(ctx.learning)
        for done, item in enumerate(items, start=1):
            try:
                picks = await self._value_one(
                    request, item, snapshots, fitted, (now, tally, overlay)
                )
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
        run: tuple[datetime, Tally, LearningOverlay],
    ) -> list[Pick]:
        ctx = self._ctx
        now, tally, learned = run
        record = item.record
        fixture = record.fixture
        kind = classify(record, now) or "upcoming"
        # A retro card shows what the model said then: today's calibration and segment rules
        # are knowledge from the future, so it always gets the raw model.
        overlay = EMPTY_OVERLAY if kind == "retro" else learned
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
                calibration=overlay.calibration,
            ),
            ctx.config,
        )
        if valuation.skip is not None:
            logger.debug("fixture_skipped", fixture=str(fixture.ref), reason=valuation.skip)
            tally.no_price += valuation.skip == SKIP_NO_PRICE
            tally.stale_price += valuation.skip == SKIP_STALE_PRICE
        watched = request.scope is QueryScope.ALL and item.watched
        # Learning loop: disabled segments and raised EV bars apply to recommendations only;
        # "show me everything" (custom) and followed fixtures keep every candidate.
        candidates = (
            valuation.candidates
            if request.include_no_value or watched
            else overlay.filter(valuation.candidates, fixture)
        )
        return picks_for_fixture(
            record, candidates, request, watched=watched, retro=kind == "retro"
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


async def _emit(progress: ProgressSink | None, stage: str, done: int, total: int) -> None:
    if progress is not None:
        await progress(stage, done, total)
