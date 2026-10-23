"""Reusable behavioural tests of the service contract (``docs/contracts.md`` §4).

Not collected on its own (no ``test_`` prefix). A concrete module imports the ``*Contract``
classes and provides three fixtures:

- ``services``: a :class:`~app.services.interfaces.Services` bundle under test;
- ``clock``: the :class:`MutableClock` that implementation reads ("now" starts on a
  Wednesday morning in Tashkent, before the day's matches);
- ``user_id``: a whitelisted user with default settings.

The suite is data-agnostic: it discovers leagues, teams and fixtures through the catalog,
so the real services can run it against any seeded database with fixtures from a week ago
to two weeks ahead.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain import MarketType, Sport
from app.services.errors import (
    BusyError,
    CooldownActiveError,
    NotFoundError,
    ValidationFailedError,
)
from app.services.interfaces import (
    BALANCED_PRESET,
    MAX_FUTURE_DAYS,
    MAX_RETRO_LOOKBACK_DAYS,
    RECALC_COOLDOWN,
    AnalysisProgress,
    AnalysisQuery,
    AnalysisResult,
    AnalysisStage,
    BetDraft,
    DateSpec,
    FixtureListRequest,
    FixtureOption,
    RecalcVerdict,
    ReportGroup,
    ReportRequest,
    ReportView,
    ResultMark,
    ResultMarkRequest,
    RiskProfile,
    ScopeKind,
    Services,
    SignalView,
    SortKey,
    TemplateDraft,
    UserSettingsView,
    WatchKind,
    WatchNotification,
    WatchTarget,
)

START = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)  # Wednesday 11:00 in Tashkent
OTHER_USER = 999_999


@dataclass
class MutableClock:
    """Injected "now" that tests move forward explicitly (no real sleeping).

    ``advance_async`` is what the contract tests call: it moves the clock and then awaits
    ``after_advance``, a hook an implementation under test sets to let its world catch up
    (the real services' harness marks the matches that have kicked off as finished). The
    in-memory fake needs no hook.
    """

    now: datetime = START
    after_advance: Callable[[datetime], Awaitable[None]] | None = None

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta

    async def advance_async(self, delta: timedelta) -> None:
        self.advance(delta)
        if self.after_advance is not None:
            await self.after_advance(self.now)


@dataclass
class ProgressRecorder:
    """Collects progress callbacks."""

    events: list[AnalysisProgress] = field(default_factory=list)

    async def __call__(self, progress: AnalysisProgress) -> None:
        self.events.append(progress)


def local_today(settings: UserSettingsView, clock: MutableClock) -> date:
    return clock().astimezone(settings.zone).date()


def all_markets() -> frozenset[MarketType]:
    return frozenset(MarketType)


async def analyze_week(services: Services, user_id: int, **overrides: object) -> AnalysisResult:
    """A broad analysis of the next week with permissive thresholds."""
    values: dict[str, object] = {
        "dates": DateSpec.next_days(7),
        "min_probability": 0.30,
        "min_ev": 0.0,
        "markets": all_markets(),
    }
    values.update(overrides)
    return await services.analysis.analyze(user_id, AnalysisQuery(**values))  # type: ignore[arg-type]


async def retro_signals(services: Services, user_id: int) -> tuple[SignalView, ...]:
    """Signals of last week's (finished) matches, via a custom retro analysis."""
    result = await services.analysis.analyze(
        user_id,
        AnalysisQuery(
            dates=DateSpec.between(*_last_week(START.date())),
            markets=all_markets(),
            include_no_value=True,
        ),
    )
    return result.signals


def _last_week(today: date) -> tuple[date, date]:
    return today - timedelta(days=7), today - timedelta(days=1)


SETTLE_DELAY = timedelta(days=4)
"""Moves "now" past every match of a ``next_days(3)`` analysis."""


async def settled_recommendations(
    services: Services, user_id: int, clock: MutableClock
) -> tuple[SignalView, ...]:
    """Recommend signals for the next three days, then let the matches finish."""
    result = await analyze_week(services, user_id, dates=DateSpec.next_days(3))
    assert result.signals, "the seeded data must yield recommendations"
    await clock.advance_async(SETTLE_DELAY)
    return tuple([await services.signals.get_signal(user_id, s.signal_id) for s in result.signals])


def assert_counts(report: ReportView) -> None:
    assert report.won + report.lost + report.void <= report.settled <= report.signals
    assert sum(bucket.count for bucket in report.calibration) == report.won + report.lost


async def first_upcoming_fixture(services: Services, user_id: int) -> FixtureOption:
    page = await services.catalog.list_fixtures(
        user_id, FixtureListRequest(dates=DateSpec.next_days(7))
    )
    assert page.items, "the seeded data must contain upcoming football fixtures"
    return page.items[0]


SORT_CHECKS: dict[SortKey, Callable[[SignalView], object]] = {
    SortKey.EV: lambda signal: -signal.ev,
    SortKey.PROBABILITY: lambda signal: -signal.probability,
    SortKey.KICKOFF: lambda signal: signal.kickoff,
}


class AnalysisContract:
    async def test_estimate_resolves_the_window_and_spends_nothing(
        self, services: Services, user_id: int, clock: MutableClock
    ) -> None:
        settings = await services.settings.get_settings(user_id)
        query = AnalysisQuery(dates=DateSpec.today())
        first = await services.analysis.estimate_cost(user_id, query)
        second = await services.analysis.estimate_cost(user_id, query)

        assert first == second
        assert first.window.date_from == first.window.date_to == local_today(settings, clock)
        assert first.fixtures_found >= 0
        assert first.api_requests >= 0
        assert first.within_budget == (first.api_requests <= first.budget_remaining)

    async def test_signals_respect_every_filter(
        self, services: Services, user_id: int, clock: MutableClock
    ) -> None:
        settings = await services.settings.get_settings(user_id)
        query = AnalysisQuery(
            dates=DateSpec.next_days(7),
            min_probability=0.40,
            min_ev=0.02,
            markets=frozenset({MarketType.MATCH_RESULT, MarketType.TOTALS}),
        )
        result = await services.analysis.analyze(user_id, query)

        assert result.signals, "permissive weekly analysis should find signals"
        assert result.matched >= len(result.signals)
        today = local_today(settings, clock)
        for signal in result.signals:
            assert signal.probability >= query.min_probability
            assert signal.ev >= query.min_ev
            assert settings.odds_min <= signal.odds <= settings.odds_max
            assert signal.market.type in query.markets
            assert signal.sport in query.sports
            assert not signal.no_value
            local_day = signal.kickoff.astimezone(settings.zone).date()
            assert today <= local_day <= today + timedelta(days=6)

    @pytest.mark.parametrize("sort", list(SortKey))
    async def test_signals_are_sorted(
        self, services: Services, user_id: int, sort: SortKey
    ) -> None:
        result = await analyze_week(services, user_id, sort=sort)
        keys = [SORT_CHECKS[sort](signal) for signal in result.signals]
        assert keys == sorted(keys)  # type: ignore[type-var]

    async def test_count_cap_and_shortfall(self, services: Services, user_id: int) -> None:
        everything = await analyze_week(services, user_id)
        capped = await analyze_week(services, user_id, max_signals=3)

        assert len(capped.signals) == min(3, everything.matched)
        assert capped.requested == 3
        assert capped.matched == everything.matched
        assert capped.signals == everything.signals[: len(capped.signals)]

    async def test_per_day_cap(self, services: Services, user_id: int) -> None:
        settings = await services.settings.get_settings(user_id)
        result = await analyze_week(services, user_id, max_signals=1, per_day=True)
        days = [signal.kickoff.astimezone(settings.zone).date() for signal in result.signals]
        assert len(days) == len(set(days))

    async def test_reanalysis_is_idempotent(self, services: Services, user_id: int) -> None:
        first = await analyze_week(services, user_id)
        second = await analyze_week(services, user_id)
        assert [s.signal_id for s in first.signals] == [s.signal_id for s in second.signals]

    async def test_progress_is_reported_in_order(self, services: Services, user_id: int) -> None:
        recorder = ProgressRecorder()
        query = AnalysisQuery(dates=DateSpec.next_days(3))
        result = await services.analysis.analyze(user_id, query, progress=recorder)

        assert recorder.events
        assert recorder.events[-1].stage is AnalysisStage.FINISHED
        assert all(event.total == result.fixtures_analyzed for event in recorder.events)
        done = [event.done for event in recorder.events]
        assert done == sorted(done)
        assert all(0 <= event.done <= event.total for event in recorder.events)

    async def test_custom_analysis_returns_no_value_selections(
        self, services: Services, user_id: int
    ) -> None:
        fixture = await first_upcoming_fixture(services, user_id)
        query = AnalysisQuery(
            dates=DateSpec.next_days(7),
            scope=ScopeKind.FIXTURES,
            scope_ids=frozenset({fixture.fixture_id}),
            markets=all_markets(),
            include_no_value=True,
        )
        result = await services.analysis.analyze(user_id, query)

        assert result.signals
        assert {signal.fixture_id for signal in result.signals} == {fixture.fixture_id}
        assert len(result.signals) > 1
        for signal in result.signals:
            if signal.no_value:
                assert signal.stake_units == 0

    async def test_league_scope(self, services: Services, user_id: int) -> None:
        league = (await services.catalog.list_leagues(Sport.FOOTBALL))[0]
        result = await analyze_week(
            services, user_id, scope=ScopeKind.LEAGUES, scope_ids=frozenset({league.league_id})
        )
        assert {signal.league for signal in result.signals} <= {league.name}

    async def test_window_beyond_the_horizon_is_rejected(
        self, services: Services, user_id: int, clock: MutableClock
    ) -> None:
        settings = await services.settings.get_settings(user_id)
        today = local_today(settings, clock)
        query = AnalysisQuery(
            dates=DateSpec.between(
                today + timedelta(days=10), today + timedelta(days=MAX_FUTURE_DAYS + 1)
            )
        )
        with pytest.raises(ValidationFailedError):
            await services.analysis.estimate_cost(user_id, query)
        with pytest.raises(ValidationFailedError):
            await services.analysis.analyze(user_id, query)

    async def test_retro_analysis_shows_results(
        self, services: Services, user_id: int, clock: MutableClock
    ) -> None:
        result = await services.analysis.analyze(
            user_id,
            AnalysisQuery(dates=DateSpec.between(*_last_week(clock().date())), min_ev=0.0),
        )
        assert result.retro
        assert result.api_requests_used == 0
        assert result.signals
        assert all(signal.kickoff < clock() for signal in result.signals)
        assert any(signal.result is not None for signal in result.signals)
        assert all(signal.retro and not signal.recommended for signal in result.signals)

    async def test_kicked_off_fixtures_never_become_real_signals(
        self, services: Services, user_id: int, clock: MutableClock
    ) -> None:
        await clock.advance_async(
            timedelta(hours=14)
        )  # 01:00 next day in Tashkent: yesterday's are over
        result = await analyze_week(services, user_id, dates=DateSpec.between(*_two_days(clock)))

        assert any(signal.retro for signal in result.signals)
        for signal in result.signals:
            if signal.kickoff <= clock():
                assert signal.retro
                assert not signal.recommended
            else:
                assert not signal.retro
                assert signal.recommended

    async def test_watched_team_is_always_included_outside_the_cap(
        self, services: Services, user_id: int
    ) -> None:
        fixture = await first_upcoming_fixture(services, user_id)
        teams = await services.catalog.search_teams(fixture.home_team, limit=5)
        team = next(team for team in teams if team.name == fixture.home_team)
        await services.watchlist.add_watch(user_id, WatchTarget(WatchKind.TEAM, team.team_id))

        strict = await analyze_week(services, user_id, min_probability=0.95, min_ev=0.20)
        capped = await analyze_week(services, user_id, max_signals=1)

        watched = [s for s in strict.signals if s.fixture_id == fixture.fixture_id]
        assert watched
        assert all(signal.watchlist for signal in watched)
        assert strict.matched == 0
        regular = [s for s in capped.signals if not s.watchlist]
        assert len(regular) == 1
        assert any(signal.watchlist for signal in capped.signals)
        assert capped.matched >= len(regular)

    async def test_scope_all_follows_the_settings_leagues(
        self, services: Services, user_id: int
    ) -> None:
        league = (await services.catalog.list_leagues(Sport.FOOTBALL))[0]
        settings = await services.settings.get_settings(user_id)
        await services.settings.update_settings(
            user_id, replace(settings, league_ids=frozenset({league.league_id}))
        )

        result = await analyze_week(services, user_id)
        page = await services.catalog.list_fixtures(
            user_id, FixtureListRequest(dates=DateSpec.next_days(7), page_size=20)
        )

        assert result.signals
        assert {signal.league for signal in result.signals} == {league.name}
        assert {item.league for item in page.items} == {league.name}

    async def test_second_concurrent_analysis_of_a_user_is_refused(
        self, services: Services, user_id: int
    ) -> None:
        started, gate = asyncio.Event(), asyncio.Event()

        async def blocking(_progress: AnalysisProgress) -> None:
            started.set()
            await gate.wait()

        first = asyncio.create_task(
            services.analysis.analyze(
                user_id, AnalysisQuery(dates=DateSpec.next_days(2)), progress=blocking
            )
        )
        await started.wait()
        try:
            with pytest.raises(BusyError):
                await services.analysis.analyze(user_id, AnalysisQuery(dates=DateSpec.today()))
            other = await services.analysis.analyze(
                OTHER_USER, AnalysisQuery(dates=DateSpec.today())
            )
            assert other.query_id
        finally:
            gate.set()
        await first
        again = await services.analysis.analyze(user_id, AnalysisQuery(dates=DateSpec.today()))
        assert again.query_id

    async def test_retro_window_starting_too_long_ago_is_rejected(
        self, services: Services, user_id: int, clock: MutableClock
    ) -> None:
        today = local_today(await services.settings.get_settings(user_id), clock)
        too_old = AnalysisQuery(
            dates=DateSpec.between(
                today - timedelta(days=MAX_RETRO_LOOKBACK_DAYS + 5),
                today - timedelta(days=MAX_RETRO_LOOKBACK_DAYS + 1),
            )
        )
        with pytest.raises(ValidationFailedError):
            await services.analysis.estimate_cost(user_id, too_old)
        with pytest.raises(ValidationFailedError):
            await services.analysis.analyze(user_id, too_old)

        oldest_allowed = AnalysisQuery(
            dates=DateSpec.between(
                today - timedelta(days=MAX_RETRO_LOOKBACK_DAYS),
                today - timedelta(days=MAX_RETRO_LOOKBACK_DAYS - 3),
            )
        )
        estimate = await services.analysis.estimate_cost(user_id, oldest_allowed)
        assert estimate.window.date_from == today - timedelta(days=MAX_RETRO_LOOKBACK_DAYS)

    async def test_failing_progress_callback_does_not_abort(
        self, services: Services, user_id: int
    ) -> None:
        async def broken(progress: AnalysisProgress) -> None:
            raise RuntimeError(progress.stage)

        result = await services.analysis.analyze(
            user_id, AnalysisQuery(dates=DateSpec.next_days(2)), progress=broken
        )
        batch = await services.analysis.recalc_query(user_id, result.query_id, progress=broken)
        assert result.fixtures_analyzed > 0
        assert batch.query_id == result.query_id


def _two_days(clock: MutableClock) -> tuple[date, date]:
    """Yesterday and today in Tashkent."""
    today = clock().astimezone(UserSettingsView().zone).date()
    return today - timedelta(days=1), today


class RecommendationContract:
    async def test_recommendation_is_sticky(self, services: Services, user_id: int) -> None:
        signal = (await analyze_week(services, user_id)).signals[0]
        assert signal.recommended

        strict = await services.analysis.analyze(
            user_id,
            AnalysisQuery(
                dates=DateSpec.next_days(7),
                scope=ScopeKind.FIXTURES,
                scope_ids=frozenset({signal.fixture_id}),
                min_probability=0.95,
                min_ev=0.20,
                markets=all_markets(),
                include_no_value=True,
            ),
        )

        again = next(s for s in strict.signals if s.signal_id == signal.signal_id)
        assert again.recommended_at == signal.recommended_at
        assert not again.no_value
        exported = (await services.reports.export(user_id)).content.decode("utf-8-sig")
        assert f"\n{signal.signal_id}," in exported

    async def test_no_value_selection_is_recommended_only_when_a_query_passes_it(
        self, services: Services, user_id: int, clock: MutableClock
    ) -> None:
        settings = await services.settings.get_settings(user_id)
        candidate, custom = await _no_value_candidate(services, user_id, settings)
        assert not candidate.recommended
        assert candidate.signal_id not in await _exported_ids(services, user_id)

        await clock.advance_async(timedelta(minutes=5))
        normal = await analyze_week(
            services,
            user_id,
            scope=ScopeKind.FIXTURES,
            scope_ids=frozenset({candidate.fixture_id}),
            markets=custom,
        )

        promoted = next(s for s in normal.signals if s.signal_id == candidate.signal_id)
        assert promoted.recommended_at == clock()
        assert not promoted.no_value
        assert candidate.signal_id in await _exported_ids(services, user_id)

    async def test_hidden_signals_still_count(
        self, services: Services, user_id: int, clock: MutableClock
    ) -> None:
        signals = await settled_recommendations(services, user_id, clock)
        request = ReportRequest.recent(14)
        before = await services.reports.report(user_id, request)
        losers = [s for s in signals if s.result is ResultMark.LOST]
        assert losers

        for signal in losers:
            await services.signals.hide_signal(user_id, signal.signal_id)
        after = await services.reports.report(user_id, request)

        assert after == before
        assert len(await _exported_ids(services, user_id)) == len(signals)

    async def test_retro_signals_never_count(
        self, services: Services, user_id: int, clock: MutableClock
    ) -> None:
        retro = await retro_signals(services, user_id)
        assert retro

        report = await services.reports.report(user_id, ReportRequest.recent(14))
        assert report.signals == 0
        assert await _exported_ids(services, user_id) == set()
        assert await services.results.pending_results(user_id) == ()
        assert (
            await services.signals.list_signals(
                user_id, DateSpec.between(*_last_week(clock().date()))
            )
            == ()
        )
        with pytest.raises(ValidationFailedError):
            await services.bets.place_bet(
                user_id, BetDraft(signal_id=retro[0].signal_id, stake=Decimal(1))
            )
        with pytest.raises(ValidationFailedError):
            await services.results.mark_result(
                user_id, ResultMarkRequest(signal_id=retro[0].signal_id, mark=ResultMark.WON)
            )

    async def test_recalc_verdict_uses_the_creation_thresholds(
        self, services: Services, user_id: int
    ) -> None:
        signal = (await analyze_week(services, user_id)).signals[0]
        settings = await services.settings.get_settings(user_id)
        await services.settings.update_settings(
            user_id, replace(settings, min_probability=0.95, min_ev=0.20)
        )

        diff = await services.analysis.recalc_signal(user_id, signal.signal_id)

        passes = (
            diff.after.probability >= 0.30
            and diff.after.ev >= 0.0
            and settings.odds_min <= diff.after.odds <= settings.odds_max
        )
        assert (diff.verdict is RecalcVerdict.STILL_VALID) == passes


async def _no_value_candidate(
    services: Services, user_id: int, settings: UserSettingsView
) -> tuple[SignalView, frozenset[MarketType]]:
    """A no-value selection that a permissive normal analysis would pick for its fixture."""
    page = await services.catalog.list_fixtures(
        user_id, FixtureListRequest(dates=DateSpec.next_days(7), page_size=20)
    )
    for fixture in page.items:
        custom = await services.analysis.analyze(
            user_id,
            AnalysisQuery(
                dates=DateSpec.next_days(7),
                scope=ScopeKind.FIXTURES,
                scope_ids=frozenset({fixture.fixture_id}),
                min_ev=0.20,
                markets=all_markets(),
                include_no_value=True,
            ),
        )
        passing = [
            s
            for s in custom.signals
            if s.no_value
            and s.probability >= 0.30
            and s.ev >= 0.0
            and settings.odds_min <= s.odds <= settings.odds_max
        ]
        if passing:
            best = max(passing, key=lambda s: s.ev)
            return best, frozenset({best.market.type})
    pytest.fail("no fixture offers a promotable no-value selection")


async def _exported_ids(services: Services, user_id: int) -> set[int]:
    lines = (
        (await services.reports.export(user_id)).content.decode("utf-8-sig").strip().splitlines()
    )
    return {int(line.split(",", 1)[0]) for line in lines[1:]}


class RecalcContract:
    async def test_recalc_updates_the_signal_and_starts_the_cooldown(
        self, services: Services, user_id: int, clock: MutableClock
    ) -> None:
        signal = (await analyze_week(services, user_id)).signals[0]

        diff = await services.analysis.recalc_signal(user_id, signal.signal_id)

        assert diff.signal.signal_id == signal.signal_id
        assert diff.before.odds == signal.odds
        assert diff.after.odds == diff.signal.odds
        assert diff.recalculated_at == clock()
        assert diff.signal.version > signal.version or diff.signal.data_as_of is not None
        with pytest.raises(CooldownActiveError) as excinfo:
            await services.analysis.recalc_signal(user_id, signal.signal_id)
        assert timedelta(0) < excinfo.value.retry_after <= RECALC_COOLDOWN

        await clock.advance_async(RECALC_COOLDOWN)
        again = await services.analysis.recalc_signal(user_id, signal.signal_id)
        assert again.before == diff.after

    async def test_recalc_of_unknown_or_foreign_signal(
        self, services: Services, user_id: int
    ) -> None:
        signal = (await analyze_week(services, user_id)).signals[0]
        with pytest.raises(NotFoundError):
            await services.analysis.recalc_signal(user_id, 10**9)
        with pytest.raises(NotFoundError):
            await services.analysis.recalc_signal(OTHER_USER, signal.signal_id)

    async def test_recalc_after_kickoff_is_rejected(self, services: Services, user_id: int) -> None:
        signal = (await retro_signals(services, user_id))[0]
        with pytest.raises(ValidationFailedError):
            await services.analysis.recalc_signal(user_id, signal.signal_id)

    async def test_recalc_query_skips_cooled_down_fixtures(
        self, services: Services, user_id: int
    ) -> None:
        result = await analyze_week(services, user_id, max_signals=4)
        first = result.signals[0]
        await services.analysis.recalc_signal(user_id, first.signal_id)
        recorder = ProgressRecorder()

        batch = await services.analysis.recalc_query(user_id, result.query_id, progress=recorder)

        assert batch.query_id == result.query_id
        assert first.signal_id in batch.skipped_cooldown
        handled = {d.signal.signal_id for d in batch.diffs} | set(batch.skipped_cooldown)
        assert handled | set(batch.skipped_started) | set(batch.failed) == {
            s.signal_id for s in result.signals
        }
        assert recorder.events[-1].stage is AnalysisStage.FINISHED

    async def test_recalc_window_needs_no_query_id(self, services: Services, user_id: int) -> None:
        result = await analyze_week(services, user_id, max_signals=4)
        recorder = ProgressRecorder()

        batch = await services.analysis.recalc_window(
            user_id, DateSpec.next_days(7), progress=recorder
        )

        handled = {d.signal.signal_id for d in batch.diffs}
        handled |= set(batch.skipped_cooldown) | set(batch.skipped_started) | set(batch.failed)
        assert handled == {s.signal_id for s in result.signals}
        assert recorder.events[-1].stage is AnalysisStage.FINISHED
        again = await services.analysis.recalc_window(user_id, DateSpec.next_days(7))
        assert again.diffs == ()
        assert set(again.skipped_cooldown) | set(again.skipped_started) == handled

    async def test_recalc_signals_skips_unknown_and_foreign_ids(
        self, services: Services, user_id: int
    ) -> None:
        first = (await analyze_week(services, user_id)).signals[0]

        batch = await services.analysis.recalc_signals(
            user_id, [first.signal_id, first.signal_id, 10**9]
        )
        assert [d.signal.signal_id for d in batch.diffs] == [first.signal_id]
        foreign = await services.analysis.recalc_signals(OTHER_USER, [first.signal_id])
        assert foreign.diffs == () and not foreign.failed

    async def test_recalc_query_ownership(self, services: Services, user_id: int) -> None:
        result = await analyze_week(services, user_id, max_signals=1)
        with pytest.raises(NotFoundError):
            await services.analysis.recalc_query(user_id, "no-such-query")
        with pytest.raises(NotFoundError):
            await services.analysis.recalc_query(OTHER_USER, result.query_id)


class FixtureDetailContract:
    async def test_detail_card(self, services: Services, user_id: int) -> None:
        fixture = await first_upcoming_fixture(services, user_id)
        detail = await services.analysis.fixture_detail(fixture.fixture_id)

        assert (detail.home_team, detail.away_team) == (fixture.home_team, fixture.away_team)
        assert len(detail.home_form) <= 5
        assert len(detail.away_form) <= 5
        assert len(detail.top_scores) <= 3
        probabilities = [score.probability for score in detail.top_scores]
        assert probabilities == sorted(probabilities, reverse=True)
        assert detail.markets
        assert all(0 < quote.probability <= 1 for quote in detail.markets)
        assert detail.home_score is None

    async def test_unknown_fixture(self, services: Services) -> None:
        with pytest.raises(NotFoundError):
            await services.analysis.fixture_detail("no-such-fixture")


class SignalContract:
    async def test_get_list_and_hide(self, services: Services, user_id: int) -> None:
        signal = (await analyze_week(services, user_id)).signals[0]

        assert await services.signals.get_signal(user_id, signal.signal_id) == signal
        week = await services.signals.list_signals(user_id, DateSpec.next_days(7))
        assert signal.signal_id in {s.signal_id for s in week}
        assert [s.kickoff for s in week] == sorted(s.kickoff for s in week)

        hidden = await services.signals.hide_signal(user_id, signal.signal_id)
        assert hidden == await services.signals.hide_signal(user_id, signal.signal_id)
        visible = await services.signals.list_signals(user_id, DateSpec.next_days(7))
        everything = await services.signals.list_signals(
            user_id, DateSpec.next_days(7), include_hidden=True
        )
        assert signal.signal_id not in {s.signal_id for s in visible}
        assert signal.signal_id in {s.signal_id for s in everything}

    async def test_get_signals_keeps_order_and_omits_unknown_ids(
        self, services: Services, user_id: int
    ) -> None:
        signals = (await analyze_week(services, user_id)).signals[:3]
        ids = [signal.signal_id for signal in reversed(signals)]

        found = await services.signals.get_signals(user_id, [*ids, 10**9])

        assert [signal.signal_id for signal in found] == ids
        assert await services.signals.get_signals(OTHER_USER, ids) == ()
        assert await services.signals.get_signals(user_id, []) == ()

    async def test_cards_carry_the_catalog_ids_of_their_teams_and_league(
        self, services: Services, user_id: int
    ) -> None:
        """v1.7: the follow buttons need no name search - the ids are the catalog's own."""
        signals = (await analyze_week(services, user_id)).signals[:3]
        assert signals

        for signal in signals:
            assert signal.home_team_id and signal.away_team_id and signal.league_id
            assert signal.home_team_id != signal.away_team_id
            for team_id, name in (
                (signal.home_team_id, signal.home_team),
                (signal.away_team_id, signal.away_team),
            ):
                found = await services.catalog.search_teams(name, sport=signal.sport)
                assert team_id in {team.team_id for team in found}
            leagues = await services.catalog.list_leagues(signal.sport)
            assert signal.league_id in {league.league_id for league in leagues}
            assert signal.league in {x.name for x in leagues if x.league_id == signal.league_id}

    async def test_foreign_signals_are_invisible(self, services: Services, user_id: int) -> None:
        signal = (await analyze_week(services, user_id)).signals[0]
        with pytest.raises(NotFoundError):
            await services.signals.get_signal(OTHER_USER, signal.signal_id)
        with pytest.raises(NotFoundError):
            await services.signals.hide_signal(OTHER_USER, signal.signal_id)
        assert await services.signals.list_signals(OTHER_USER, DateSpec.next_days(7)) == ()


class CatalogContract:
    async def test_leagues_are_sorted_by_name(self, services: Services) -> None:
        leagues = await services.catalog.list_leagues(Sport.FOOTBALL)
        assert leagues
        assert all(league.sport is Sport.FOOTBALL for league in leagues)
        assert [league.name for league in leagues] == sorted(league.name for league in leagues)

    async def test_team_search(self, services: Services, user_id: int) -> None:
        fixture = await first_upcoming_fixture(services, user_id)
        found = await services.catalog.search_teams(fixture.home_team, sport=Sport.FOOTBALL)
        assert found[0].name == fixture.home_team
        with pytest.raises(ValidationFailedError):
            await services.catalog.search_teams("a")
        with pytest.raises(ValidationFailedError):
            await services.catalog.search_teams("arsenal", limit=0)

    async def test_fixture_search_by_both_team_names(
        self, services: Services, user_id: int
    ) -> None:
        fixture = await first_upcoming_fixture(services, user_id)
        found = await services.catalog.search_fixtures(f"{fixture.home_team} - {fixture.away_team}")
        assert fixture.fixture_id in {option.fixture_id for option in found}

    async def test_fixture_pages(self, services: Services, user_id: int) -> None:
        request = FixtureListRequest(dates=DateSpec.next_days(7), page_size=5)
        first = await services.catalog.list_fixtures(user_id, request)
        assert first.total > 5
        assert first.page_count == -(-first.total // 5)
        pages = [first] + [
            await services.catalog.list_fixtures(user_id, replace(request, page=page))
            for page in range(1, first.page_count)
        ]
        items = [item for page in pages for item in page.items]
        assert len(items) == first.total == len({item.fixture_id for item in items})
        assert [item.kickoff for item in items] == sorted(item.kickoff for item in items)
        past_end = await services.catalog.list_fixtures(
            user_id, replace(request, page=first.page_count)
        )
        assert past_end.items == ()

    async def test_fixture_filters(self, services: Services, user_id: int) -> None:
        fixture = await first_upcoming_fixture(services, user_id)
        teams = await services.catalog.search_teams(fixture.away_team)
        team_id = next(team.team_id for team in teams if team.name == fixture.away_team)
        page = await services.catalog.list_fixtures(
            user_id,
            FixtureListRequest(dates=DateSpec.next_days(7), team_ids=frozenset({team_id})),
        )
        assert page.items
        assert all(fixture.away_team in (i.home_team, i.away_team) for i in page.items)


class SettingsContract:
    async def test_new_user_gets_defaults(self, services: Services) -> None:
        assert await services.settings.get_settings(123_456) == UserSettingsView()

    async def test_update_persists_and_rederives_the_profile(
        self, services: Services, user_id: int
    ) -> None:
        current = await services.settings.get_settings(user_id)
        edited = await services.settings.update_settings(
            user_id, replace(current, min_probability=0.55, bank=Decimal(1000), unit=Decimal(10))
        )
        assert edited.risk_profile is RiskProfile.CUSTOM
        assert await services.settings.get_settings(user_id) == edited

        back = await services.settings.update_settings(
            user_id, replace(edited, min_probability=BALANCED_PRESET.min_probability)
        )
        assert back.risk_profile is RiskProfile.BALANCED

    async def test_apply_risk_profile(self, services: Services, user_id: int) -> None:
        settings = await services.settings.apply_risk_profile(user_id, RiskProfile.AGGRESSIVE)
        assert (settings.min_probability, settings.min_ev) == (0.30, 0.08)
        assert (settings.odds_min, settings.odds_max) == (2.00, 5.00)
        assert await services.settings.get_settings(user_id) == settings


class TemplateContract:
    async def test_save_replace_list_delete(self, services: Services, user_id: int) -> None:
        query = AnalysisQuery(dates=DateSpec.weekend())
        saved = await services.templates.save_template(
            user_id, TemplateDraft(name="Dam olish", query=query)
        )
        replaced = await services.templates.save_template(
            user_id, TemplateDraft(name="Dam olish", query=replace(query, min_ev=0.1))
        )
        other = await services.templates.save_template(
            user_id, TemplateDraft(name="Bugun", query=AnalysisQuery(dates=DateSpec.today()))
        )

        assert replaced.template_id == saved.template_id
        assert replaced.query.min_ev == 0.1
        assert [t.name for t in await services.templates.list_templates(user_id)] == [
            "Bugun",
            "Dam olish",
        ]
        await services.templates.delete_template(user_id, other.template_id)
        assert [t.template_id for t in await services.templates.list_templates(user_id)] == [
            saved.template_id
        ]
        with pytest.raises(NotFoundError):
            await services.templates.delete_template(user_id, other.template_id)
        with pytest.raises(NotFoundError):
            await services.templates.delete_template(OTHER_USER, saved.template_id)


class WatchlistContract:
    async def test_add_notify_remove(self, services: Services) -> None:
        user_id = 4242
        league = (await services.catalog.list_leagues(Sport.FOOTBALL))[0]
        target = WatchTarget(WatchKind.LEAGUE, league.league_id)

        item = await services.watchlist.add_watch(user_id, target)
        assert await services.watchlist.add_watch(user_id, target) == item
        assert item.name == league.name
        updated = await services.watchlist.set_notifications(
            user_id, target, frozenset({WatchNotification.RESULT})
        )
        assert updated.notifications == {WatchNotification.RESULT}
        assert await services.watchlist.list_watchlist(user_id) == (updated,)
        assert await services.watchlist.remove_watch(user_id, target)
        assert not await services.watchlist.remove_watch(user_id, target)
        with pytest.raises(NotFoundError):
            await services.watchlist.set_notifications(user_id, target, frozenset())

    async def test_unknown_entity(self, services: Services, user_id: int) -> None:
        with pytest.raises(NotFoundError):
            await services.watchlist.add_watch(user_id, WatchTarget(WatchKind.TEAM, "no-team"))


class BetContract:
    async def test_bet_needs_a_stake_or_a_unit(self, services: Services, user_id: int) -> None:
        signal = (await analyze_week(services, user_id)).signals[0]
        with pytest.raises(ValidationFailedError):
            await services.bets.place_bet(user_id, BetDraft(signal_id=signal.signal_id))
        with pytest.raises(NotFoundError):
            await services.bets.place_bet(OTHER_USER, BetDraft(signal_id=signal.signal_id))

    async def test_place_list_and_bank(self, services: Services, user_id: int) -> None:
        settings = await services.settings.get_settings(user_id)
        await services.settings.update_settings(
            user_id, replace(settings, bank=Decimal(1000), unit=Decimal(10))
        )
        signal = (await analyze_week(services, user_id)).signals[0]

        bet = await services.bets.place_bet(user_id, BetDraft(signal_id=signal.signal_id))

        assert bet.odds_taken == signal.odds
        assert bet.stake == Decimal(10) * signal.stake_units
        assert bet.outcome is None
        assert await services.bets.list_bets(user_id, open_only=True) == (bet,)
        bank = await services.bets.bank_summary(user_id)
        assert bank.balance == Decimal(1000)
        assert bank.open_bets == 1
        assert not bank.loss_limit_reached

    async def test_placing_with_the_same_token_twice_records_one_bet(
        self, services: Services, user_id: int
    ) -> None:
        signal = (await analyze_week(services, user_id)).signals[0]
        draft = BetDraft(signal_id=signal.signal_id, stake=Decimal(5), client_token="tap-1")

        first = await services.bets.place_bet(user_id, draft)
        second = await services.bets.place_bet(user_id, draft)
        other = await services.bets.place_bet(user_id, replace(draft, client_token="tap-2"))

        assert second == first
        assert other.bet_id != first.bet_id
        assert len(await services.bets.list_bets(user_id)) == 2

    async def test_balance_is_the_starting_bank_plus_settled_profit(
        self, services: Services, user_id: int, clock: MutableClock
    ) -> None:
        settings = await services.settings.get_settings(user_id)
        await services.settings.update_settings(
            user_id, replace(settings, bank=Decimal(1000), unit=Decimal(10))
        )
        signal = (await analyze_week(services, user_id, dates=DateSpec.next_days(3))).signals[0]
        await services.bets.place_bet(
            user_id, BetDraft(signal_id=signal.signal_id, odds_taken=2.0, stake=Decimal(10))
        )
        assert (await services.bets.bank_summary(user_id)).balance == Decimal(1000)

        await clock.advance_async(SETTLE_DELAY)

        (bet,) = await services.bets.list_bets(user_id)
        bank = await services.bets.bank_summary(user_id)
        assert bank.balance == Decimal(1000) + (bet.profit or Decimal(0))
        assert bank.open_bets == (1 if bet.outcome is None else 0)
        if bet.profit is not None:
            assert bank.history[-1].balance == bank.balance

    async def test_settled_bet_has_profit(
        self, services: Services, user_id: int, clock: MutableClock
    ) -> None:
        signal = (await analyze_week(services, user_id, dates=DateSpec.next_days(3))).signals[0]
        await services.bets.place_bet(
            user_id, BetDraft(signal_id=signal.signal_id, odds_taken=2.0, stake=Decimal(10))
        )
        await clock.advance_async(SETTLE_DELAY)

        settled = await services.signals.get_signal(user_id, signal.signal_id)
        (bet,) = await services.bets.list_bets(user_id)
        assert bet.outcome == settled.result
        assert (bet.profit is None) == (settled.result is None)

    async def test_get_bet_returns_exactly_the_users_bet(
        self, services: Services, user_id: int
    ) -> None:
        signal = (await analyze_week(services, user_id)).signals[0]
        bet = await services.bets.place_bet(
            user_id, BetDraft(signal_id=signal.signal_id, stake=Decimal(5))
        )

        assert await services.bets.get_bet(user_id, bet.bet_id) == bet
        with pytest.raises(NotFoundError):
            await services.bets.get_bet(OTHER_USER, bet.bet_id)  # another user's bet
        with pytest.raises(NotFoundError):
            await services.bets.get_bet(user_id, bet.bet_id + 10_000)

    async def test_bank_totals_count_every_settled_bet_not_only_a_page(
        self, services: Services, user_id: int, clock: MutableClock
    ) -> None:
        signal = (await analyze_week(services, user_id, dates=DateSpec.next_days(3))).signals[0]
        for _ in range(105):  # more than list_bets can return
            await services.bets.place_bet(
                user_id, BetDraft(signal_id=signal.signal_id, odds_taken=2.0, stake=Decimal(1))
            )
        await clock.advance_async(SETTLE_DELAY)
        await services.results.mark_result(
            user_id, ResultMarkRequest(signal_id=signal.signal_id, mark=ResultMark.WON)
        )

        bank = await services.bets.bank_summary(user_id)

        assert bank.totals.settled == 105
        assert bank.totals.won == 105
        (one,) = await services.bets.list_bets(user_id, limit=1)
        assert one.profit is not None
        assert bank.totals.profit == one.profit * 105  # every bet is identical
        assert bank.totals.staked == Decimal(105)
        assert bank.totals.yield_rate == pytest.approx(float(one.profit))
        assert bank.open_bets == 0


class ResultContract:
    async def test_pending_and_manual_marks(
        self, services: Services, user_id: int, clock: MutableClock
    ) -> None:
        signals = await settled_recommendations(services, user_id, clock)
        pending = await services.results.pending_results(user_id)
        assert {s.signal_id for s in pending} == {s.signal_id for s in signals if s.result is None}

        target = signals[0]
        marked = await services.results.mark_result(
            user_id,
            ResultMarkRequest(
                signal_id=target.signal_id,
                mark=ResultMark.CASHOUT,
                cashout_profit_units=Decimal("0.4"),
            ),
        )
        assert marked.result is ResultMark.CASHOUT
        assert target.signal_id not in {
            s.signal_id for s in await services.results.pending_results(user_id)
        }

    async def test_cashout_payout_makes_the_bet_profit_payout_minus_stake(
        self, services: Services, user_id: int, clock: MutableClock
    ) -> None:
        settings = await services.settings.get_settings(user_id)
        await services.settings.update_settings(
            user_id, replace(settings, bank=Decimal(10_000), unit=Decimal(100))
        )
        signal = (await analyze_week(services, user_id, dates=DateSpec.next_days(3))).signals[0]
        cashed = await services.bets.place_bet(
            user_id, BetDraft(signal_id=signal.signal_id, odds_taken=2.0, stake=Decimal(250))
        )
        other = await services.bets.place_bet(
            user_id, BetDraft(signal_id=signal.signal_id, odds_taken=2.0, stake=Decimal(100))
        )
        await clock.advance_async(SETTLE_DELAY)

        marked = await services.results.mark_result(
            user_id,
            ResultMarkRequest(
                signal_id=signal.signal_id,
                mark=ResultMark.CASHOUT,
                cashout_bet_id=cashed.bet_id,
                cashout_payout=Decimal(200),
            ),
        )

        assert marked.result is ResultMark.CASHOUT
        assert (await services.bets.get_bet(user_id, cashed.bet_id)).profit == Decimal(-50)
        # the other bet on the signal follows at the same return (-20%), not at "-50"
        assert (await services.bets.get_bet(user_id, other.bet_id)).profit == Decimal(-20)
        assert (await services.bets.bank_summary(user_id)).totals.cashout == 2

    async def test_cashout_payout_for_a_foreign_or_unknown_bet_is_refused(
        self, services: Services, user_id: int, clock: MutableClock
    ) -> None:
        signal = (await analyze_week(services, user_id, dates=DateSpec.next_days(3))).signals[0]
        await clock.advance_async(SETTLE_DELAY)

        with pytest.raises(NotFoundError):
            await services.results.mark_result(
                user_id,
                ResultMarkRequest(
                    signal_id=signal.signal_id,
                    mark=ResultMark.CASHOUT,
                    cashout_bet_id=424_242,
                    cashout_payout=Decimal(10),
                ),
            )

    async def test_future_signals_cannot_be_marked(self, services: Services, user_id: int) -> None:
        signal = (await analyze_week(services, user_id)).signals[0]
        with pytest.raises(ValidationFailedError):
            await services.results.mark_result(
                user_id, ResultMarkRequest(signal_id=signal.signal_id, mark=ResultMark.WON)
            )

    async def test_notes(self, services: Services, user_id: int) -> None:
        signal = (await analyze_week(services, user_id)).signals[0]
        await services.results.add_note(user_id, signal.signal_id, "Asosiy hujumchi yo'q edi")
        for text in ("   ", "x" * 501):
            with pytest.raises(ValidationFailedError):
                await services.results.add_note(user_id, signal.signal_id, text)
        with pytest.raises(NotFoundError):
            await services.results.add_note(OTHER_USER, signal.signal_id, "izoh")

    async def test_day_summary_is_consistent(
        self, services: Services, user_id: int, clock: MutableClock
    ) -> None:
        settings = await services.settings.get_settings(user_id)
        signals = await settled_recommendations(services, user_id, clock)
        day = signals[0].kickoff.astimezone(settings.zone).date()

        summary = await services.results.day_summary(user_id, day)

        same_day = [s for s in signals if s.kickoff.astimezone(settings.zone).date() == day]
        assert summary.day == day
        assert summary.signals == len(same_day)
        assert summary.won + summary.lost + summary.void + summary.pending <= summary.signals


class ReportContract:
    async def test_report_metrics_are_consistent(
        self, services: Services, user_id: int, clock: MutableClock
    ) -> None:
        signals = await settled_recommendations(services, user_id, clock)
        request = ReportRequest.recent(14, group_by=ReportGroup.LEAGUE)

        report = await services.reports.report(user_id, request)

        assert report.signals == len(signals)
        assert report.settled > 0
        assert_counts(report)
        assert report.date_to == clock().astimezone(UserSettingsView().zone).date()
        assert report.date_from == report.date_to - timedelta(days=13)
        assert sum(segment.signals for segment in report.segments) == report.settled
        explicit = await services.reports.report(
            user_id,
            ReportRequest(
                date_from=report.date_from, date_to=report.date_to, group_by=ReportGroup.LEAGUE
            ),
        )
        assert explicit == report
        if report.win_rate is not None:
            assert 0 <= report.win_rate <= 1
        assert report.longest_losing_streak <= report.lost

    async def test_empty_report(self, services: Services) -> None:
        report = await services.reports.report(54_321, ReportRequest(date_to=START.date()))
        assert (report.signals, report.settled, report.calibration) == (0, 0, ())
        assert report.yield_rate is None
        assert report.roi is None
        all_time = await services.reports.report(54_321, ReportRequest.all_time())
        assert all_time.date_from is None

    async def test_roi_counts_the_users_bets(
        self, services: Services, user_id: int, clock: MutableClock
    ) -> None:
        signal = (await analyze_week(services, user_id, dates=DateSpec.next_days(3))).signals[0]
        await services.bets.place_bet(
            user_id, BetDraft(signal_id=signal.signal_id, odds_taken=2.0, stake=Decimal(10))
        )
        await clock.advance_async(SETTLE_DELAY)
        (bet,) = await services.bets.list_bets(user_id)

        report = await services.reports.report(user_id, ReportRequest.recent(14))

        if bet.profit is None:
            assert report.roi is None
        else:
            assert report.roi == pytest.approx(float(bet.profit / bet.stake))

    async def test_export_is_csv(self, services: Services, user_id: int) -> None:
        result = await analyze_week(services, user_id)
        exported = await services.reports.export(user_id)
        lines = exported.content.decode("utf-8-sig").strip().splitlines()
        assert exported.filename.endswith(".csv")
        assert lines[0].startswith("signal_id,")
        assert len(lines) == 1 + len(result.signals)


class ExportBetsContract:
    async def test_bets_export_is_csv_with_one_line_per_bet(
        self, services: Services, user_id: int
    ) -> None:
        empty = await services.reports.export_bets(user_id)
        assert empty.content.decode("utf-8-sig").strip().splitlines() == [
            "bet_id,signal_id,fixture,market,line,selection,odds_taken,stake,placed_at,outcome,profit"
        ]
        signals = (await analyze_week(services, user_id, dates=DateSpec.next_days(3))).signals
        for signal in signals[:2]:
            await services.bets.place_bet(
                user_id, BetDraft(signal_id=signal.signal_id, odds_taken=2.0, stake=Decimal(10))
            )

        exported = await services.reports.export_bets(user_id)
        lines = exported.content.decode("utf-8-sig").strip().splitlines()

        assert exported.filename == "bets.csv"
        assert exported.media_type == "text/csv"
        assert len(lines) == 1 + min(2, len(signals))
        assert await services.reports.export_bets(OTHER_USER) != exported  # per user


class ExpressContract:
    async def picks_of_different_matches(
        self, services: Services, user_id: int, count: int
    ) -> list[SignalView]:
        signals = (await analyze_week(services, user_id, dates=DateSpec.next_days(7))).signals
        by_fixture = {signal.fixture_id: signal for signal in signals if signal.recommended}
        assert len(by_fixture) >= count
        return list(by_fixture.values())[:count]

    async def test_probability_odds_and_ev_multiply(self, services: Services, user_id: int) -> None:
        picks = await self.picks_of_different_matches(services, user_id, 3)

        express = await services.express.evaluate(user_id, [pick.signal_id for pick in picks])

        probability = picks[0].probability * picks[1].probability * picks[2].probability
        odds = picks[0].odds * picks[1].odds * picks[2].odds
        assert [leg.signal_id for leg in express.legs] == [pick.signal_id for pick in picks]
        assert express.combined_probability == pytest.approx(probability)
        assert express.combined_odds == pytest.approx(odds)
        assert express.ev == pytest.approx(probability * odds - 1)
        assert express.fair_odds == pytest.approx(1 / probability)

    async def test_two_picks_of_one_match_are_refused(
        self, services: Services, user_id: int
    ) -> None:
        result = await analyze_week(
            services, user_id, dates=DateSpec.next_days(3), include_no_value=True
        )
        recommended = [signal for signal in result.signals if signal.recommended]
        twins = next(
            (a, b)
            for a in recommended
            for b in recommended
            if a.signal_id < b.signal_id and a.fixture_id == b.fixture_id
        )

        with pytest.raises(ValidationFailedError, match="correlated"):
            await services.express.evaluate(user_id, [twins[0].signal_id, twins[1].signal_id])

    async def test_leg_count_duplicates_and_unknown_signals(
        self, services: Services, user_id: int
    ) -> None:
        picks = await self.picks_of_different_matches(services, user_id, 5)
        ids = [pick.signal_id for pick in picks]

        for bad in ([], ids[:1], ids, [ids[0], ids[0]]):
            with pytest.raises(ValidationFailedError):
                await services.express.evaluate(user_id, bad)
        with pytest.raises(NotFoundError):
            await services.express.evaluate(user_id, [ids[0], 987_654])
        with pytest.raises(NotFoundError):  # somebody else's signals do not exist for us
            await services.express.evaluate(OTHER_USER, ids[:2])
        assert (await services.express.evaluate(user_id, ids[:4])).legs  # the maximum is fine

    async def test_started_matches_cannot_be_combined(
        self, services: Services, user_id: int, clock: MutableClock
    ) -> None:
        picks = await self.picks_of_different_matches(services, user_id, 2)
        await clock.advance_async(timedelta(days=8))

        with pytest.raises(ValidationFailedError):
            await services.express.evaluate(user_id, [pick.signal_id for pick in picks])


class SportsContract:
    async def test_every_sport_is_listed_and_football_is_available(
        self, services: Services
    ) -> None:
        sports = await services.catalog.list_sports()
        by_sport = {option.sport: option for option in sports}

        assert [option.sport for option in sports] == list(Sport)
        football = by_sport[Sport.FOOTBALL]
        assert football.available
        assert not football.experimental
        assert MarketType.MATCH_RESULT in football.supported_markets
        for option in sports:
            assert option.available == bool(option.supported_markets)


class StatusContract:
    async def test_status(self, services: Services) -> None:
        status = await services.status.system_status()
        assert status.quotas
        assert all(quota.remaining >= 0 for quota in status.quotas)
        assert all(job.name for job in status.jobs)
