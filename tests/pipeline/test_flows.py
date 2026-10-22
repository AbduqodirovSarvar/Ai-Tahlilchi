"""Failure modes and edge cases of the jobs, over the scripted provider and seeded database."""

from dataclasses import replace
from datetime import time, timedelta
from decimal import Decimal

import pytest

from app.db.enums import Outcome, SignalStatus
from app.db.repo import get_signal, upsert_live
from app.domain import FixtureStatus, Sport
from app.pipeline.analyze import AnalysisEngine
from app.pipeline.daily import run_due_digests
from app.pipeline.errors import ProviderUnavailableError
from app.pipeline.prematch import run_due_recalcs
from app.pipeline.profile import default_request, load_profile
from app.pipeline.refresh import refresh
from app.pipeline.settle import run_settlement
from app.services.errors import BudgetExceededError, ServiceUnavailableError
from app.services.interfaces import (
    AnalysisQuery,
    DateSpec,
    QuietHours,
    ResultMark,
    ResultMarkRequest,
    ScopeKind,
    WatchKind,
    WatchNotification,
    WatchTarget,
)
from tests.pipeline.harness import Env
from tests.pipeline.providers import ScriptedProvider

USER = 2002
PERMISSIVE = {"min_probability": 0.30, "min_ev": 0.0}


def scripted(env: Env) -> ScriptedProvider:
    provider = env.ctx.providers[Sport.FOOTBALL]
    assert isinstance(provider, ScriptedProvider)
    return provider


async def tune(env: Env, **changes: object) -> None:
    settings = await env.system.services.settings.get_settings(USER)
    values = {"min_probability": 0.30, "min_ev": 0.0, "quiet_hours": None, **changes}
    await env.system.services.settings.update_settings(USER, replace(settings, **values))


async def recommend(env: Env, days: int = 1) -> list[int]:
    result = await env.system.services.analysis.analyze(
        USER, AnalysisQuery(dates=DateSpec.next_days(days), **PERMISSIVE)
    )
    assert result.signals
    return [s.signal_id for s in result.signals]


# ------------------------------------------------------------------------------ refresh
async def test_refresh_without_a_provider_is_an_error(seeded_env: Env) -> None:
    with pytest.raises(ProviderUnavailableError):
        await refresh(seeded_env.ctx)


async def test_refresh_with_a_spent_budget_skips_every_step(provider_env: Env) -> None:
    scripted(provider_env).exhausted = True

    summary = await refresh(provider_env.ctx)

    assert summary.skipped == ["fixtures"]
    assert not summary.ok
    runs = await provider_env.ctx.ledger.last_runs()
    assert not runs["refresh"].ok
    status = await provider_env.system.services.status.system_status()
    assert [job.name for job in status.jobs] == ["refresh"]
    assert not status.jobs[0].ok


async def test_refresh_survives_a_provider_outage_and_can_be_forced(provider_env: Env) -> None:
    provider = scripted(provider_env)
    # The real adapter wrapper translates provider errors; jobs see pipeline errors.
    provider.fail_with = ProviderUnavailableError("api-football", "down")

    broken = await refresh(provider_env.ctx)
    assert broken.skipped == ["fixtures"]

    provider.fail_with = None
    forced = await refresh(provider_env.ctx, force=True)
    assert forced.ok
    assert forced.fixtures > 0
    status = await provider_env.system.services.status.system_status()
    assert status.last_refresh_at is not None
    assert status.quotas[0].limit == 100


async def test_refresh_fetches_results_of_finished_matches(provider_env: Env) -> None:
    await refresh(provider_env.ctx)
    provider_env.clock.advance(timedelta(days=1))
    summary = await refresh(provider_env.ctx)
    assert summary.results > 0


# ------------------------------------------------------------------------------ analysis
async def test_estimate_then_manual_analysis_spends_then_is_cached(provider_env: Env) -> None:
    services = provider_env.system.services
    query = AnalysisQuery(dates=DateSpec.next_days(2), **PERMISSIVE)
    estimate = await services.analysis.estimate_cost(USER, query)
    assert estimate.api_requests > 0
    assert estimate.within_budget

    result = await services.analysis.analyze(USER, query)

    assert result.signals
    purposes = {purpose for _, purpose in scripted(provider_env).calls}
    assert purposes == {"manual"}
    assert (await services.analysis.estimate_cost(USER, query)).api_requests == 0


async def test_manual_analysis_with_a_spent_reserve_is_refused(provider_env: Env) -> None:
    services = provider_env.system.services
    scripted(provider_env).exhausted = True
    query = AnalysisQuery(dates=DateSpec.next_days(2), **PERMISSIVE)

    estimate = await services.analysis.estimate_cost(USER, query)
    assert not estimate.within_budget
    with pytest.raises(BudgetExceededError):
        await services.analysis.analyze(USER, query)


async def test_a_provider_outage_is_reported_to_the_bot(provider_env: Env) -> None:
    scripted(provider_env).fail_with = ProviderUnavailableError("api-football", "down")
    with pytest.raises(ServiceUnavailableError):
        await provider_env.system.services.analysis.analyze(
            USER, AnalysisQuery(dates=DateSpec.next_days(2), **PERMISSIVE)
        )


async def test_automatic_analysis_lets_a_failed_fetch_pass(provider_env: Env) -> None:
    scripted(provider_env).fail_with = ProviderUnavailableError("api-football", "down")
    ctx = provider_env.ctx
    async with ctx.sessions() as session:
        loaded = await load_profile(session, USER)
    today = ctx.clock().date()

    outcome = await AnalysisEngine(ctx).run(default_request(loaded, today, today))

    assert outcome.cards == ()
    assert outcome.fixtures_analyzed == 0


# ------------------------------------------------------------------------------ daily
async def test_a_digest_waits_for_quiet_hours_to_end(provider_env: Env) -> None:
    await tune(provider_env, quiet_hours=QuietHours(time(10, 0), time(12, 0)))
    await refresh(provider_env.ctx)

    held = await run_due_digests(provider_env.ctx, provider_env.system.delivery)
    assert held.held == (USER,)
    assert provider_env.notifier.sent == []

    provider_env.clock.advance(timedelta(hours=1, minutes=5))
    sent = await run_due_digests(provider_env.ctx, provider_env.system.delivery)
    assert sent.sent == (USER,)
    assert len(provider_env.notifier.sent) == 1


async def test_a_digest_missed_for_too_long_is_skipped(provider_env: Env) -> None:
    await tune(provider_env)
    provider_env.clock.advance(timedelta(hours=14, minutes=30))  # 16.5 h after 09:00 local

    report = await run_due_digests(provider_env.ctx, provider_env.system.delivery)

    assert report.sent == ()
    assert provider_env.notifier.sent == []


async def test_a_failed_digest_is_retried_on_the_next_tick(
    provider_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await tune(provider_env)
    original = AnalysisEngine.run

    async def broken(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(AnalysisEngine, "run", broken)
    failed = await run_due_digests(provider_env.ctx, provider_env.system.delivery)
    assert failed.failed == (USER,)
    assert [e[1] for e in await provider_env.ctx.ledger.recent_errors()] == ["daily"]

    monkeypatch.setattr(AnalysisEngine, "run", original)
    retried = await run_due_digests(provider_env.ctx, provider_env.system.delivery)
    assert retried.sent == (USER,)


# ------------------------------------------------------------------------------ prematch
async def test_updates_during_quiet_hours_are_not_sent_but_numbers_stay_fresh(
    provider_env: Env,
) -> None:
    await tune(provider_env, quiet_hours=QuietHours(time(0), time(23, 59)))
    await refresh(provider_env.ctx)
    ids = await recommend(provider_env)

    silent = notified = 0
    for _ in range(24):
        provider_env.clock.advance(timedelta(minutes=30))
        report = await run_due_recalcs(provider_env.ctx, provider_env.system.delivery)
        silent += report.silent
        notified += report.notified

    assert notified == 0
    assert silent > 0
    assert provider_env.notifier.sent == []
    views = await provider_env.system.services.signals.get_signals(USER, ids)
    assert any(view.version > 1 for view in views)


async def test_a_postponed_match_cancels_its_signals_and_tells_the_user(
    provider_env: Env,
) -> None:
    await tune(provider_env)
    await refresh(provider_env.ctx)
    ids = await recommend(provider_env)
    victim = (await provider_env.system.services.signals.get_signal(USER, ids[0])).fixture_id
    async with provider_env.ctx.sessions() as session:
        record = await get_signal(session, ids[0])
    assert record is not None
    postponed = replace(record.fixture.fixture, status=FixtureStatus.POSTPONED)
    async with provider_env.ctx.sessions.begin() as session:
        await upsert_live(session, fixtures=[postponed])

    notified = 0
    for _ in range(24):
        provider_env.clock.advance(timedelta(minutes=30))
        report = await run_due_recalcs(provider_env.ctx, provider_env.system.delivery)
        notified += report.notified

    assert notified > 0
    cancelled = [s for s in provider_env.notifier.sent if s.method == "signal_cancelled"]
    assert cancelled
    view = await provider_env.system.services.signals.get_signal(USER, ids[0])
    assert view.state.value == "cancelled"
    assert view.fixture_id == victim


async def test_watchlist_notifications_follow_line_ups_and_odds_moves(provider_env: Env) -> None:
    await tune(provider_env)
    services = provider_env.system.services
    league = next(
        option
        for option in await services.catalog.list_leagues(Sport.FOOTBALL)
        if option.name == "Premier League"
    )
    target = WatchTarget(WatchKind.LEAGUE, league.league_id)
    await services.watchlist.add_watch(USER, target)
    await services.watchlist.set_notifications(
        USER, target, frozenset({WatchNotification.LINEUPS, WatchNotification.ODDS_MOVE})
    )
    await refresh(provider_env.ctx)
    await recommend(provider_env)
    provider = scripted(provider_env)
    provider.lineups = True
    provider.odds = [replace(snapshot, odds=snapshot.odds * 1.25) for snapshot in provider.odds]

    for _ in range(24):
        provider_env.clock.advance(timedelta(minutes=30))
        await run_due_recalcs(provider_env.ctx, provider_env.system.delivery)

    events = [s for s in provider_env.notifier.sent if s.method == "watchlist_event"]
    kinds = {event.payload.kind.value for event in events}  # type: ignore[attr-defined]
    assert kinds == {"lineups", "odds_move"}


async def test_a_hidden_signal_is_not_recalculated(provider_env: Env) -> None:
    await tune(provider_env)
    await refresh(provider_env.ctx)
    ids = await recommend(provider_env)
    for signal_id in ids:
        await provider_env.system.services.signals.hide_signal(USER, signal_id)
    provider_env.clock.advance(timedelta(hours=12))

    report = await run_due_recalcs(provider_env.ctx, provider_env.system.delivery)

    assert report.notified == 0
    assert report.skipped > 0


# ------------------------------------------------------------------------------ recalc
async def test_recalculation_without_budget_keeps_the_numbers_and_dates_them(
    provider_env: Env,
) -> None:
    services = provider_env.system.services
    await tune(provider_env)
    await refresh(provider_env.ctx)
    (first, *_) = await recommend(provider_env)
    scripted(provider_env).exhausted = True

    diff = await services.analysis.recalc_signal(USER, first)

    assert diff.signal.data_as_of is not None
    assert diff.after.odds == diff.before.odds


# ------------------------------------------------------------------------------ settle
async def test_a_manual_result_is_not_overwritten_by_the_api(seeded_env: Env) -> None:
    services = seeded_env.system.services
    await tune(seeded_env)
    ids = await recommend(seeded_env)
    await seeded_env.clock.advance_async(timedelta(hours=14))
    marked = await services.results.mark_result(
        USER, ResultMarkRequest(signal_id=ids[0], mark=ResultMark.VOID)
    )
    assert marked.result is ResultMark.VOID

    await run_settlement(seeded_env.ctx, seeded_env.system.delivery)

    assert (await services.signals.get_signal(USER, ids[0])).result is ResultMark.VOID


async def test_a_cancelled_match_voids_its_signals(seeded_env: Env) -> None:
    await tune(seeded_env)
    ids = await recommend(seeded_env)
    async with seeded_env.ctx.sessions() as session:
        record = await get_signal(session, ids[0])
    assert record is not None
    async with seeded_env.ctx.sessions.begin() as session:
        await upsert_live(
            session, fixtures=[replace(record.fixture.fixture, status=FixtureStatus.CANCELLED)]
        )
    seeded_env.clock.advance(timedelta(days=1))

    report = await run_settlement(seeded_env.ctx, seeded_env.system.delivery)

    assert report.settled >= 1
    async with seeded_env.ctx.sessions() as session:
        stored = await get_signal(session, ids[0])
    assert stored is not None
    assert stored.result is not None
    assert stored.result.outcome is Outcome.VOID
    assert stored.result.profit_units == Decimal(0)
    assert stored.status is SignalStatus.ACTIVE


async def test_settlement_waits_for_missing_results(provider_env: Env) -> None:
    await tune(provider_env)
    await refresh(provider_env.ctx)
    await recommend(provider_env)
    provider_env.clock.advance(timedelta(hours=12))  # past kick-off + 2 h of the early matches
    scripted(provider_env).results.clear()

    report = await run_settlement(provider_env.ctx, provider_env.system.delivery)

    assert report.settled == 0
    assert report.waiting > 0


async def test_settlement_survives_a_provider_outage(provider_env: Env) -> None:
    await tune(provider_env)
    await refresh(provider_env.ctx)
    await recommend(provider_env)
    provider_env.clock.advance(timedelta(hours=12))
    scripted(provider_env).fail_with = ProviderUnavailableError("api-football", "down")

    report = await run_settlement(provider_env.ctx, provider_env.system.delivery)

    assert report.fetch_failed
    assert report.settled == 0


async def test_scope_leagues_and_fixtures_through_the_engine(seeded_env: Env) -> None:
    services = seeded_env.system.services
    league = (await services.catalog.list_leagues(Sport.FOOTBALL))[0]
    result = await services.analysis.analyze(
        USER,
        AnalysisQuery(
            dates=DateSpec.next_days(7),
            scope=ScopeKind.LEAGUES,
            scope_ids=frozenset({league.league_id}),
            **PERMISSIVE,
        ),
    )
    assert {signal.league for signal in result.signals} <= {league.name}
