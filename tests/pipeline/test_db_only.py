"""Decision #25: analyses read the database only; the refresh and the admin refresh fill it."""

import asyncio
import itertools
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.db.repo import list_fixtures, odds_history
from app.domain import ExternalRef, Sport
from app.pipeline.analyze import AnalysisEngine
from app.pipeline.config import PipelineConfig
from app.pipeline.daily import run_due_digests
from app.pipeline.errors import (
    BudgetExhaustedError,
    ProviderUnavailableError,
    RefreshTooSoonError,
)
from app.pipeline.freshness import last_refresh_at
from app.pipeline.profile import default_request, load_profile
from app.pipeline.refresh import local_utc_days, refresh, refresh_manual
from app.services.errors import (
    BudgetExceededError,
    CooldownActiveError,
    DataStaleError,
    NotAuthorizedError,
    ServiceUnavailableError,
)
from app.services.interfaces import AnalysisQuery, DateSpec
from app.services.real.browser import RealDataBrowserService
from app.services.real.core import Core
from tests.pipeline.harness import Env, open_env
from tests.pipeline.providers import ScriptedProvider, scripted_provider
from tests.pipeline.world import START, World

USER = 2002
ADMIN = 1
PERMISSIVE = {"min_probability": 0.30, "min_ev": 0.0}
TASHKENT = ZoneInfo("Asia/Tashkent")
DAY = timedelta(days=1)
NAIVE_FROM = datetime(2024, 1, 1)  # noqa: DTZ001
NAIVE_TO = datetime(2027, 12, 31)  # noqa: DTZ001


def scripted(env: Env) -> ScriptedProvider:
    provider = env.ctx.providers[Sport.FOOTBALL]
    assert isinstance(provider, ScriptedProvider)
    return provider


def query(days: int = 2) -> AnalysisQuery:
    return AnalysisQuery(dates=DateSpec.next_days(days), **PERMISSIVE)


@pytest.fixture
async def stale_env(history_template: tuple[Path, World], tmp_path: Path) -> AsyncIterator[Env]:
    """History only, a scripted provider and a database that was never refreshed."""
    template, world = history_template
    async with open_env(
        template,
        world,
        tmp_path / "stale.db",
        provider_factory=lambda clock: scripted_provider(world, clock),
        reveal_results=False,
        fresh=False,
    ) as opened:
        yield opened


def browser_for(env: Env, admin_ids: frozenset[int] | None) -> RealDataBrowserService:
    return RealDataBrowserService(Core(env.ctx, admin_ids=admin_ids))


# ------------------------------------------------------------------------------ staleness
async def test_a_never_refreshed_database_refuses_a_manual_analysis(stale_env: Env) -> None:
    with pytest.raises(DataStaleError) as caught:
        await stale_env.system.services.analysis.analyze(USER, query())

    assert caught.value.last_refresh_at is None
    assert scripted(stale_env).calls == []  # the refusal costs no API request


async def test_the_estimate_reports_a_stale_database_and_costs_nothing(stale_env: Env) -> None:
    estimate = await stale_env.system.services.analysis.estimate_cost(USER, query())

    assert estimate.data_stale
    assert estimate.last_refresh_at is None
    assert estimate.api_requests == 0
    assert scripted(stale_env).calls == []


async def test_a_look_back_needs_no_refresh(stale_env: Env) -> None:
    past = AnalysisQuery(dates=DateSpec.between(date(2026, 10, 14), date(2026, 10, 15)))

    result = await stale_env.system.services.analysis.analyze(USER, past)

    assert result.retro
    assert not result.data_stale
    assert scripted(stale_env).calls == []


async def test_the_refresh_makes_the_database_fresh_until_the_age_limit(stale_env: Env) -> None:
    await refresh(stale_env.ctx)
    stamp = await last_refresh_at(stale_env.ctx)
    assert stamp == START
    limit = stale_env.ctx.config.max_quote_age

    stale_env.clock.advance(limit)  # exactly at the limit: still fresh
    estimate = await stale_env.system.services.analysis.estimate_cost(USER, query(1))
    assert not estimate.data_stale

    stale_env.clock.advance(timedelta(seconds=1))
    with pytest.raises(DataStaleError) as caught:
        await stale_env.system.services.analysis.analyze(USER, query(1))
    assert caught.value.last_refresh_at == START


async def test_an_automatic_run_on_a_stale_database_goes_on_and_says_so(stale_env: Env) -> None:
    ctx = stale_env.ctx
    async with ctx.sessions() as session:
        profile = await load_profile(session, USER)
    today = ctx.clock().date()

    outcome = await AnalysisEngine(ctx).run(default_request(profile, today, today))

    assert outcome.data_stale
    assert scripted(stale_env).calls == []


async def test_a_refresh_that_lost_a_required_step_does_not_count(stale_env: Env) -> None:
    scripted(stale_env).fail_with = ProviderUnavailableError("api-football", "down")

    summary = await refresh(stale_env.ctx)

    assert "fixtures" in summary.skipped
    assert await last_refresh_at(stale_env.ctx) is None
    scripted(stale_env).fail_with = None
    await refresh(stale_env.ctx, force=True)
    assert await last_refresh_at(stale_env.ctx) == START


# ------------------------------------------------------------------------------ no provider calls
async def test_a_recalculation_and_a_digest_never_call_the_provider(stale_env: Env) -> None:
    services = stale_env.system.services
    await refresh(stale_env.ctx)
    result = await services.analysis.analyze(USER, query())
    assert result.signals
    calls = list(scripted(stale_env).calls)

    diff = await services.analysis.recalc_signal(USER, result.signals[0].signal_id)
    await run_due_digests(stale_env.ctx, stale_env.system.delivery)

    assert diff.signal.signal_id == result.signals[0].signal_id
    assert scripted(stale_env).calls == calls


# ------------------------------------------------------------------------------ manual refresh
async def test_the_manual_refresh_stores_stamps_and_counts_its_requests(stale_env: Env) -> None:
    report = await refresh_manual(stale_env.ctx)

    provider = scripted(stale_env)
    assert report.fixtures > 0
    assert report.odds > 0
    assert report.skipped == ()
    assert report.api_requests == len(provider.calls)
    assert {purpose for _, purpose in provider.calls} == {"manual"}
    assert await last_refresh_at(stale_env.ctx) == START
    assert (await stale_env.ctx.ledger.last_runs())["refresh"].detail.startswith("manual ")


async def test_a_second_manual_refresh_waits_for_the_minimum_gap(stale_env: Env) -> None:
    await refresh_manual(stale_env.ctx)
    gap = stale_env.ctx.config.manual_refresh_min_interval
    calls = len(scripted(stale_env).calls)

    stale_env.clock.advance(timedelta(minutes=10))
    with pytest.raises(RefreshTooSoonError) as caught:
        await refresh_manual(stale_env.ctx)
    assert caught.value.retry_after == gap - timedelta(minutes=10)
    assert len(scripted(stale_env).calls) == calls

    stale_env.clock.advance(gap - timedelta(minutes=10))
    await refresh_manual(stale_env.ctx)
    assert len(scripted(stale_env).calls) > calls


async def test_two_simultaneous_manual_refreshes_run_once(stale_env: Env) -> None:
    outcomes = await asyncio.gather(
        refresh_manual(stale_env.ctx), refresh_manual(stale_env.ctx), return_exceptions=True
    )

    reports = [o for o in outcomes if not isinstance(o, BaseException)]
    refused = [o for o in outcomes if isinstance(o, RefreshTooSoonError)]
    assert (len(reports), len(refused)) == (1, 1)


async def test_a_refresh_the_quota_cannot_cover_sends_nothing_and_frees_the_gap(
    stale_env: Env,
) -> None:
    provider = scripted(stale_env)
    provider.exhausted = True

    with pytest.raises(BudgetExhaustedError):
        await refresh_manual(stale_env.ctx)
    assert provider.calls == []

    provider.exhausted = False
    await refresh_manual(stale_env.ctx)  # no waiting: the refused attempt released the claim


async def test_a_provider_outage_skips_everything_and_does_not_lock_the_button(
    stale_env: Env,
) -> None:
    provider = scripted(stale_env)
    provider.fail_with = ProviderUnavailableError("api-football", "down")

    report = await refresh_manual(stale_env.ctx)

    assert "fixtures" in report.skipped
    assert await last_refresh_at(stale_env.ctx) is None
    provider.fail_with = None
    assert (await refresh_manual(stale_env.ctx)).skipped == ()


async def test_a_cancelled_manual_refresh_frees_the_gap(
    stale_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()

    async def hang(*_: object, **__: object) -> list[object]:
        started.set()
        await asyncio.Event().wait()
        return []

    with monkeypatch.context() as patch:
        patch.setattr("tests.pipeline.providers.ScriptedAdapter.fetch_fixtures", hang)
        task = asyncio.ensure_future(refresh_manual(stale_env.ctx))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert (await refresh_manual(stale_env.ctx)).skipped == ()


async def test_the_manual_refresh_without_a_provider_is_an_error_and_keeps_the_gap_free(
    seeded_env: Env,
) -> None:
    with pytest.raises(ProviderUnavailableError):
        await refresh_manual(seeded_env.ctx)
    with pytest.raises(ProviderUnavailableError):  # still no RefreshTooSoon: nothing was claimed
        await refresh_manual(seeded_env.ctx)


# ------------------------------------------------------------------------------ admin service
async def test_only_an_administrator_may_refresh_the_database(stale_env: Env) -> None:
    browser = browser_for(stale_env, frozenset({ADMIN}))

    with pytest.raises(NotAuthorizedError):
        await browser.refresh_database(USER)

    assert scripted(stale_env).calls == []
    report = await browser.refresh_database(ADMIN)
    assert report.fixtures > 0
    assert report.complete
    assert report.started_at.tzinfo is not None


async def test_the_admin_refresh_maps_its_failures_to_service_errors(stale_env: Env) -> None:
    browser = browser_for(stale_env, None)
    provider = scripted(stale_env)
    provider.exhausted = True
    with pytest.raises(BudgetExceededError):
        await browser.refresh_database(ADMIN)
    provider.exhausted = False

    await browser.refresh_database(ADMIN)
    with pytest.raises(CooldownActiveError) as caught:
        await browser.refresh_database(ADMIN)
    assert caught.value.retry_after > timedelta(0)


async def test_the_admin_refresh_without_a_provider_is_unavailable(seeded_env: Env) -> None:
    with pytest.raises(ServiceUnavailableError):
        await browser_for(seeded_env, None).refresh_database(ADMIN)


# ------------------------------------------------------------------------------ local-day cover
def test_a_local_day_reaches_into_the_previous_utc_day() -> None:
    # 02:00 in Tashkent on 21 Oct = 21:00 UTC on 20 Oct; three local days end on 23 Oct 19:00 UTC.
    now = datetime(2026, 10, 20, 21, 0, tzinfo=UTC)

    assert local_utc_days(TASHKENT, now, 3) == [
        date(2026, 10, 20),
        date(2026, 10, 21),
        date(2026, 10, 22),
        date(2026, 10, 23),
    ]
    # The old rule ("today in UTC, N days") stopped at 22 Oct and lost the last local day.


def test_a_local_day_always_spans_two_utc_days_east_of_greenwich() -> None:
    now = datetime(2026, 10, 20, 19, 0, tzinfo=UTC)  # 00:00 on 21 Oct, local

    assert local_utc_days(TASHKENT, now, 1) == [date(2026, 10, 20), date(2026, 10, 21)]


def test_in_utc_a_local_day_is_one_utc_day() -> None:
    now = datetime(2026, 10, 20, 19, 0, tzinfo=UTC)

    assert local_utc_days(ZoneInfo("UTC"), now, 2) == [date(2026, 10, 20), date(2026, 10, 21)]


@given(
    zone=st.sampled_from(
        [
            ZoneInfo(name)
            for name in (
                "Asia/Tashkent",
                "Europe/London",
                "America/New_York",
                "Pacific/Kiritimati",
                "Pacific/Pago_Pago",
                "Australia/Lord_Howe",
            )
        ]
    ),
    now=st.datetimes(
        min_value=NAIVE_FROM,  # hypothesis wants naive bounds; the zone comes from ``timezones``
        max_value=NAIVE_TO,
        timezones=st.just(UTC),
    ),
    local_days=st.integers(min_value=1, max_value=7),
    offset=st.floats(min_value=0, max_value=1, exclude_max=True),
)
def test_every_instant_of_the_local_days_falls_on_a_covered_utc_day(
    zone: ZoneInfo, now: datetime, local_days: int, offset: float
) -> None:
    days = local_utc_days(zone, now, local_days)
    today = now.astimezone(zone).date()
    first = datetime.combine(today, datetime.min.time(), tzinfo=zone)
    last = datetime.combine(today + timedelta(days=local_days), datetime.min.time(), tzinfo=zone)
    span = (last - first) // timedelta(microseconds=1)
    probe = (first + timedelta(microseconds=min(int(span * offset), span - 1))).astimezone(UTC)

    assert days == sorted(set(days))
    assert all(b - a == DAY for a, b in itertools.pairwise(days))
    assert probe.date() in days
    assert len(days) <= local_days + 1


async def test_the_refresh_fetches_the_local_days_not_the_utc_ones(stale_env: Env) -> None:
    await refresh(stale_env.ctx)

    query_sent = scripted(stale_env).queries[0]
    # START is 11:00 on 21 Oct in Tashkent: local days 21..23 = 20 Oct 19:00Z .. 23 Oct 19:00Z.
    assert (query_sent.date_from, query_sent.date_to) == (date(2026, 10, 20), date(2026, 10, 23))


async def test_odds_are_fetched_for_the_local_odds_window_only(stale_env: Env) -> None:
    provider = scripted(stale_env)
    template = provider.fixtures[0]
    inside = replace(template, ref=ExternalRef(template.ref.source, "inside"))
    outside = replace(template, ref=ExternalRef(template.ref.source, "outside"))
    inside = replace(inside, kickoff=datetime(2026, 10, 22, 18, 0, tzinfo=UTC))  # 23:00 local
    outside = replace(outside, kickoff=datetime(2026, 10, 22, 20, 0, tzinfo=UTC))  # 01:00 next
    provider.fixtures.extend([inside, outside])
    sample = next(s for s in provider.odds if s.fixture == template.ref)
    for clone in (inside, outside):
        provider.odds.append(replace(sample, fixture=clone.ref))
        provider.kickoffs[clone.ref] = clone.kickoff

    await refresh(stale_env.ctx)

    async with stale_env.ctx.sessions() as session:
        with_odds = {
            ref.external_id: bool(await odds_history(session, ref))
            for ref in (inside.ref, outside.ref)
        }
        stored = await list_fixtures(
            session, start=START, end=START + timedelta(days=4), sport=Sport.FOOTBALL
        )
    assert with_odds == {"inside": True, "outside": False}
    assert {"inside", "outside"} <= {r.fixture.ref.external_id for r in stored.items}


def test_the_refresh_time_and_gap_are_configurable() -> None:
    config = PipelineConfig(manual_refresh_min_interval=timedelta(minutes=5))

    assert config.manual_refresh_min_interval == timedelta(minutes=5)
    assert PipelineConfig().manual_refresh_min_interval == timedelta(minutes=30)
    assert PipelineConfig().max_quote_age == timedelta(hours=30)


# ------------------------------------------------------------------------------ settings
def test_the_settings_carry_the_refresh_time_and_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import Settings  # noqa: PLC0415 - only these tests read settings

    monkeypatch.delenv("REFRESH_AT", raising=False)
    monkeypatch.delenv("MANUAL_REFRESH_MIN_MINUTES", raising=False)
    defaults = Settings(_env_file=None)
    monkeypatch.setenv("REFRESH_AT", "06:15")
    monkeypatch.setenv("MANUAL_REFRESH_MIN_MINUTES", "5")
    configured = Settings(_env_file=None)

    assert (defaults.refresh_at, defaults.manual_refresh_min_minutes) == (time(8, 30), 30)
    assert (configured.refresh_at, configured.manual_refresh_min_minutes) == (time(6, 15), 5)


async def test_the_runtime_hands_the_settings_to_the_pipeline_config(tmp_path: Path) -> None:
    from app.config import Settings  # noqa: PLC0415
    from app.pipeline.runtime import open_runtime  # noqa: PLC0415

    settings = Settings(
        _env_file=None,
        db_path=tmp_path / "runtime.db",
        refresh_at=time(6, 15),
        manual_refresh_min_minutes=5,
        display_tz="Europe/London",
    )

    async with open_runtime(settings) as runtime:
        config = runtime.ctx.config

    assert config.refresh_at == time(6, 15)
    assert config.manual_refresh_min_interval == timedelta(minutes=5)
    assert config.display_zone == ZoneInfo("Europe/London")
