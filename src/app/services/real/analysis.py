"""Real :class:`~app.services.interfaces.AnalysisService` on top of the pipeline.

The service owns the rules that belong to a *user action*: the window rules (horizon, retro
look-back), one running analysis per user, the manual-reserve budget, the 10-minute recalc
cooldown per fixture and ownership. The calculation itself is the pipeline's single analysis
path (:class:`~app.pipeline.analyze.AnalysisEngine`).
"""

import json
from collections.abc import Sequence
from typing import Final
from uuid import uuid4

from app.domain import OddsKind
from app.logging import get_logger
from app.pipeline import data
from app.pipeline.data import QueryScope, SignalVersionReason, SortOrder
from app.pipeline.data import SignalSource as StoredSource
from app.pipeline.detail import VenueStats, build_fixture_card
from app.pipeline.errors import BudgetExhaustedError, ProviderUnavailableError
from app.pipeline.ledger import STAMP_DAY, LedgerKey
from app.pipeline.profile import UserProfile
from app.pipeline.provider import MANUAL
from app.pipeline.recalc import RecalcOptions, RecalcResult, RecalcVerdict, recalc_signals
from app.pipeline.types import AnalysisOutcome, AnalysisRequest
from app.services.errors import (
    BudgetExceededError,
    BusyError,
    CooldownActiveError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationFailedError,
)
from app.services.interfaces import (
    RECALC_COOLDOWN,
    AnalysisProgress,
    AnalysisQuery,
    AnalysisResult,
    AnalysisStage,
    ConfidenceLevel,
    CostEstimate,
    DateSpec,
    DateWindow,
    EntityId,
    FixtureDetail,
    FormResult,
    HeadToHead,
    MarketQuote,
    OddsMove,
    ProgressCallback,
    RecalcBatch,
    RecalcDiff,
    ScopeKind,
    ScoreProbability,
    SignalId,
    SignalNumbers,
    SortKey,
    TeamStats,
)
from app.services.interfaces import (
    RecalcVerdict as ServiceVerdict,
)
from app.services.real.core import Core, parse_ids, require_signal, to_view

logger = get_logger(__name__)

QUERY_JOB: Final = "query"
COOLDOWN_JOB: Final = "recalc-cooldown"
_SCOPES: Final = {
    ScopeKind.ALL: QueryScope.ALL,
    ScopeKind.LEAGUES: QueryScope.LEAGUES,
    ScopeKind.TEAMS: QueryScope.TEAMS,
    ScopeKind.FIXTURES: QueryScope.FIXTURES,
}
_SORTS: Final = {
    SortKey.EV: SortOrder.EV,
    SortKey.PROBABILITY: SortOrder.PROBABILITY,
    SortKey.KICKOFF: SortOrder.KICKOFF,
}
_VERDICTS: Final = {
    RecalcVerdict.STILL_VALID: ServiceVerdict.STILL_VALID,
    RecalcVerdict.BELOW_THRESHOLD: ServiceVerdict.BELOW_THRESHOLD,
    RecalcVerdict.CANCELLED: ServiceVerdict.CANCELLED,
}
_FORMS: Final = {"win": FormResult.WIN, "draw": FormResult.DRAW, "loss": FormResult.LOSS}


async def _report(
    progress: ProgressCallback | None, stage: AnalysisStage, done: int, total: int
) -> None:
    """Call the progress callback; its failures are logged and never abort the job."""
    if progress is None:
        return
    try:
        await progress(AnalysisProgress(stage=stage, done=done, total=total))
    except Exception as error:
        logger.warning("progress_callback_failed", stage=stage, error=repr(error))


def window_of(core: Core, dates: DateSpec, profile: UserProfile) -> DateWindow:
    """The window for ``dates`` in the user's zone, checked against horizon and look-back."""
    today = core.local_today(profile.zone)
    window = dates.resolve(today)
    window.ensure_allowed(today)
    return window


def build_request(
    core: Core, profile: UserProfile, query: AnalysisQuery, window: DateWindow
) -> AnalysisRequest:
    """The pipeline request for ``query`` under the user's settings."""
    s = profile.settings
    start, end = window.utc_bounds(profile.zone)
    ids = parse_ids(query.scope_ids, "scope_ids")
    retro = window.is_retro(core.local_today(profile.zone))
    return AnalysisRequest(
        user_id=profile.user_id,
        start=start,
        end=end,
        zone=profile.zone,
        sports=query.sports,
        markets=query.markets,
        min_probability=query.min_probability,
        min_ev=query.min_ev,
        odds_min=s.odds_min,
        odds_max=s.odds_max,
        scope=_SCOPES[query.scope],
        league_ids=ids if query.scope is ScopeKind.LEAGUES else frozenset(),
        team_ids=ids if query.scope is ScopeKind.TEAMS else frozenset(),
        fixture_ids=ids if query.scope is ScopeKind.FIXTURES else frozenset(),
        default_league_ids=frozenset(s.leagues),
        watch_league_ids=profile.watch_league_ids,
        watch_team_ids=profile.watch_team_ids,
        max_signals=query.max_signals,
        per_day=query.per_day,
        sort=_SORTS[query.sort],
        include_no_value=query.include_no_value,
        source=StoredSource.CUSTOM if query.include_no_value else StoredSource.ANALYSIS,
        bank=profile.bank,
        unit=profile.unit,
        prematch_minutes=s.prematch_minutes,
        purpose=MANUAL,
        fetch=not retro,
    )


class RealAnalysisService:
    """:class:`~app.services.interfaces.AnalysisService` over the pipeline."""

    def __init__(self, core: Core) -> None:
        """Share ``core`` with the other real services."""
        self._core = core

    async def estimate_cost(self, user_id: int, query: AnalysisQuery) -> CostEstimate:
        """See :meth:`AnalysisService.estimate_cost`."""
        profile = await self._core.profile(user_id)
        window = window_of(self._core, query.dates, profile)
        request = build_request(self._core, profile, query, window)
        plan = await self._core.engine.plan(request)
        return CostEstimate(
            window=window,
            fixtures_found=plan.fixtures_found,
            api_requests=plan.api_requests,
            budget_remaining=plan.budget_remaining,
        )

    async def analyze(
        self, user_id: int, query: AnalysisQuery, *, progress: ProgressCallback | None = None
    ) -> AnalysisResult:
        """See :meth:`AnalysisService.analyze` (one at a time per user)."""
        if user_id in self._core.busy:
            raise BusyError(user_id)
        self._core.busy.add(user_id)
        try:
            return await self._analyze(user_id, query, progress)
        finally:
            self._core.busy.discard(user_id)

    async def _analyze(
        self, user_id: int, query: AnalysisQuery, progress: ProgressCallback | None
    ) -> AnalysisResult:
        core = self._core
        profile = await core.profile(user_id)
        window = window_of(core, query.dates, profile)
        request = build_request(core, profile, query, window)
        await core.ensure_user(user_id)

        async def sink(stage: str, done: int, total: int) -> None:
            await _report(progress, AnalysisStage(stage), done, total)

        try:
            outcome = await core.engine.run(request, progress=sink)
        except BudgetExhaustedError as error:
            raise BudgetExceededError(error.needed, error.available) from error
        except ProviderUnavailableError as error:
            raise ServiceUnavailableError(error.provider, error.detail) from error
        query_id = await self._remember(user_id, outcome)
        return AnalysisResult(
            query_id=query_id,
            window=window,
            signals=tuple(to_view(core, card.record, card=card) for card in outcome.cards),
            fixtures_analyzed=outcome.fixtures_analyzed,
            matched=outcome.matched,
            requested=outcome.requested,
            api_requests_used=outcome.api_requests_used,
            failed_fixtures=outcome.failed_fixtures,
            retro=window.is_retro(core.local_today(profile.zone)),
        )

    async def _remember(self, user_id: int, outcome: AnalysisOutcome) -> EntityId:
        """Store the signal ids of a run under a fresh query id (survives restarts)."""
        query_id = f"q{uuid4().hex[:12]}"
        ids = json.dumps([card.record.id for card in outcome.cards])
        await self._core.ctx.ledger.complete(
            LedgerKey(QUERY_JOB, STAMP_DAY, user_id, query_id), now=self._core.now(), detail=ids
        )
        return query_id

    # ------------------------------------------------------------------ recalculation
    async def recalc_signal(self, user_id: int, signal_id: SignalId) -> RecalcDiff:
        """See :meth:`AnalysisService.recalc_signal`."""
        record = await require_signal(self._core, user_id, signal_id)
        if record.retro or record.fixture.fixture.kickoff <= self._core.now():
            raise ValidationFailedError("signal_id", "the match has already started")
        await self._claim_cooldown(record.fixture.id)
        results, failed = await recalc_signals(self._core.ctx, [record], _manual_options())
        if failed or not results:
            raise ServiceUnavailableError("recalculation", f"signal {signal_id}")
        return self._diff(results[0])

    async def recalc_query(
        self, user_id: int, query_id: EntityId, *, progress: ProgressCallback | None = None
    ) -> RecalcBatch:
        """See :meth:`AnalysisService.recalc_query`."""
        entry = await self._core.ctx.ledger.get(LedgerKey(QUERY_JOB, STAMP_DAY, user_id, query_id))
        if entry is None or entry.detail is None:
            raise NotFoundError("query", query_id)
        ids = tuple(json.loads(entry.detail))
        return await self._batch(user_id, query_id, ids, progress)

    async def recalc_window(
        self, user_id: int, dates: DateSpec, *, progress: ProgressCallback | None = None
    ) -> RecalcBatch:
        """See :meth:`AnalysisService.recalc_window`."""
        profile = await self._core.profile(user_id)
        window = window_of(self._core, dates, profile)
        start, end = window.utc_bounds(profile.zone)
        async with self._core.ctx.sessions() as session:
            records = await data.report_signals(session, user_id, start=start, end=end)
        ids = tuple(record.id for record in records)
        return await self._batch(user_id, f"w:{window.date_from}:{window.date_to}", ids, progress)

    async def recalc_signals(
        self,
        user_id: int,
        signal_ids: Sequence[SignalId],
        *,
        progress: ProgressCallback | None = None,
    ) -> RecalcBatch:
        """See :meth:`AnalysisService.recalc_signals`."""
        ids = tuple(dict.fromkeys(signal_ids))
        return await self._batch(user_id, f"s:{len(ids)}", ids, progress)

    async def _batch(
        self,
        user_id: int,
        query_id: EntityId,
        ids: Sequence[SignalId],
        progress: ProgressCallback | None,
    ) -> RecalcBatch:
        """Recalculate ``ids``: one recalculation per fixture, cooldown/started skipped."""
        core = self._core
        records = []
        async with core.ctx.sessions() as session:
            for signal_id in ids:
                record = await data.get_signal(session, signal_id, user_id=user_id)
                if record is not None and not record.retro:
                    records.append(record)
        now = core.now()
        started = [r.id for r in records if r.fixture.fixture.kickoff <= now]
        live = [r for r in records if r.fixture.fixture.kickoff > now]
        claimed: dict[int, bool] = {}
        runnable: list[data.SignalRecord] = []
        cooled: list[data.SignalRecord] = []
        for record in live:
            fixture_id = record.fixture.id
            if fixture_id not in claimed:
                claimed[fixture_id] = await self._try_claim(fixture_id)
            (runnable if claimed[fixture_id] else cooled).append(record)
        await _report(progress, AnalysisStage.ANALYSING, 0, len(records))
        results, failed = await recalc_signals(core.ctx, runnable, _manual_options())
        await _report(progress, AnalysisStage.ANALYSING, len(records), len(records))
        await _report(progress, AnalysisStage.FINISHED, len(records), len(records))
        return RecalcBatch(
            query_id=query_id,
            diffs=tuple(self._diff(result) for result in results),
            skipped_cooldown=tuple(r.id for r in cooled),
            skipped_started=tuple(started),
            failed=tuple(failed),
        )

    async def _claim_cooldown(self, fixture_id: int) -> None:
        """Start the fixture's cooldown or raise how long is left."""
        ledger, now = self._core.ctx.ledger, self._core.now()
        last = await ledger.stamped_at(COOLDOWN_JOB, str(fixture_id))
        if last is not None and now - last < RECALC_COOLDOWN:
            raise CooldownActiveError(retry_after=RECALC_COOLDOWN - (now - last))
        await ledger.stamp(COOLDOWN_JOB, str(fixture_id), now=now)

    async def _try_claim(self, fixture_id: int) -> bool:
        try:
            await self._claim_cooldown(fixture_id)
        except CooldownActiveError:
            return False
        return True

    def _diff(self, result: RecalcResult) -> RecalcDiff:
        return recalc_diff(self._core, result)

    # ------------------------------------------------------------------ detail
    async def fixture_detail(self, fixture_id: EntityId) -> FixtureDetail:
        """See :meth:`AnalysisService.fixture_detail`."""
        try:
            number = int(fixture_id)
        except ValueError:
            raise NotFoundError("fixture", fixture_id) from None
        card = await build_fixture_card(self._core.ctx, number)
        if card is None:
            raise NotFoundError("fixture", fixture_id)
        fixture = card.record.fixture
        result = card.record.result
        return FixtureDetail(
            fixture_id=fixture_id,
            sport=fixture.sport,
            league=fixture.league.name,
            home_team=fixture.home.name,
            away_team=fixture.away.name,
            kickoff=fixture.kickoff,
            home_form=tuple(_FORMS[mark] for mark in card.home_form),
            away_form=tuple(_FORMS[mark] for mark in card.away_form),
            home_stats=_stats(card.home_stats),
            away_stats=_stats(card.away_stats),
            head_to_head=tuple(
                HeadToHead(
                    kickoff=m.kickoff,
                    home_team=m.home,
                    away_team=m.away,
                    home_score=m.home_score,
                    away_score=m.away_score,
                )
                for m in card.head_to_head
            ),
            absences=(),
            top_scores=tuple(
                ScoreProbability(home_goals=h, away_goals=a, probability=p)
                for h, a, p in card.top_scores
            ),
            markets=tuple(
                MarketQuote(
                    market=q.market,
                    selection=q.selection,
                    probability=q.probability,
                    odds=q.odds,
                    ev=q.ev,
                )
                for q in card.quotes
                if 0 < q.probability <= 1
            ),
            odds_moves=tuple(
                OddsMove(
                    market=m.market, selection=m.selection, opening=m.opening, current=m.current
                )
                for m in card.odds_moves
            ),
            home_score=None if result is None else result.home_score,
            away_score=None if result is None else result.away_score,
            data_as_of=card.data_as_of,
        )


def _stats(stats: VenueStats) -> TeamStats:
    return TeamStats(
        matches=stats.matches,
        goals_for_avg=stats.goals_for,
        goals_against_avg=stats.goals_against,
        xg_for_avg=stats.xg_for,
        xg_against_avg=stats.xg_against,
    )


def recalc_diff(core: Core, result: RecalcResult) -> RecalcDiff:
    """The contract diff of a pipeline recalculation result."""
    return RecalcDiff(
        signal=to_view(core, result.after.record, card=result.after),
        before=_numbers(result.before.record),
        after=_numbers(result.after.record),
        verdict=_VERDICTS[result.verdict],
        recalculated_at=result.recalculated_at,
    )


def _numbers(record: data.SignalRecord) -> SignalNumbers:
    return SignalNumbers(
        odds=record.odds,
        probability=record.p_model,
        ev=record.ev,
        stake_units=record.stake_units,
        confidence=ConfidenceLevel(record.confidence.value),
    )


def _manual_options() -> RecalcOptions:
    return RecalcOptions(
        reason=SignalVersionReason.RECALC, purpose=MANUAL, odds_kind=OddsKind.RECALC
    )
