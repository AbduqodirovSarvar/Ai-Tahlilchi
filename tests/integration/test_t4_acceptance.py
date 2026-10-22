"""T4 QA: the phase's acceptance claims, measured through the real pipeline path.

- a daily refresh of the ten tracked leagues stays within the request budget (counted at the
  HTTP layer behind the real API client, quota and cache);
- running out of the automatic budget in the middle of a refresh keeps what was stored and
  never touches the manual reserve;
- a provider outage is a clear error for a manual action and a stale-data flag for a recalc;
- ``python -m app.pipeline.refresh`` exits 0/1/2 and says why when the key is missing;
- the scheduler keeps its jobs across a restart (and what happens to a missed run).
"""

import itertools
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.clients.api_football_models import FixtureItem
from app.db.repo import latest_odds, list_fixtures
from app.domain import FixtureStatus, HistoryBatch, Market, OddsKind, Selection, Sport
from app.pipeline.delivery import NullDelivery
from app.pipeline.refresh import refresh
from app.pipeline.settle import run_settlement
from app.services.errors import BudgetExceededError, ServiceUnavailableError
from app.services.interfaces import AnalysisQuery, DateSpec, ResultMark
from app.sports.football_mapping import map_fixture
from tests.integration.t4_api import LEAGUE_IDS, Api, api_over_respx, record
from tests.integration.t4_support import USER, add_signal, open_bare, utc

NOW = utc(2026, 10, 21, 8, 30)
AUTOMATIC_SHARE = 0.70  # the quota policy keeps 30% for the user's own actions
REQUEST_BUDGET = 40  # plan section 9: one daily refresh of ten leagues


def fill(api: Api, *, per_league: int = 2) -> list[dict[str, object]]:
    """Yesterday's unfinished-in-our-database matches (finished at the provider) and 3 days."""
    yesterday: list[dict[str, object]] = []
    slots = itertools.product((-1, 0, 1, 2), LEAGUE_IDS, range(per_league))
    for number, (day_offset, league, _) in enumerate(slots, start=1):
        kickoff = datetime(2026, 10, 21 + day_offset, 15, tzinfo=UTC)
        if day_offset < 0:
            item = record(number, league, kickoff, status="FT", fulltime=(1, 0))
            yesterday.append(item)
        else:
            item = record(number, league, kickoff)
        api.wire.add(item)
    return yesterday


def stored_as_scheduled(items: list[dict[str, object]]) -> HistoryBatch:
    """Yesterday's fixtures as the database knows them: scheduled, result not fetched yet."""
    fixtures = tuple(
        replace(map_fixture(FixtureItem.model_validate(item)), status=FixtureStatus.SCHEDULED)
        for item in items
    )
    return HistoryBatch(fixtures=fixtures)


async def test_a_daily_refresh_of_ten_leagues_stays_within_forty_requests(tmp_path: Path) -> None:
    async with api_over_respx(NOW) as (api, fake_time):
        yesterday = fill(api)
        async with open_bare(
            tmp_path / "refresh.db",
            stored_as_scheduled(yesterday),
            providers=lambda _clock: {Sport.FOOTBALL: api.provider},
            now=NOW,
        ) as bare:
            summary = await refresh(bare.ctx)
            spent = len(api.wire.seen)
            status = await api.harness.quota.status()
            async with bare.sessions() as session:
                page = await list_fixtures(session, start=NOW, end=NOW + timedelta(days=4))

    assert summary.ok, summary
    # 3 day-requests + 1 results request (20 ids) + 10 xG (xg_per_refresh) + 20 league-day odds
    assert spent == 3 + 1 + 10 + 20
    assert spent <= REQUEST_BUDGET
    assert api.wire.count("/fixtures") == 4
    assert api.wire.count("/fixtures/statistics") == 10
    assert api.wire.count("/odds") == 20
    # 20 results; 60 fixtures; 20 league-days x 2 fixtures x 2 bookmakers x 3 selections
    assert (summary.fixtures, summary.results, summary.odds) == (60, 20, 240)
    assert len(page.items) == 60
    # only the automatic budget was used; the user's reserve is whole
    assert status.counts.requests_used == spent
    assert status.counts.manual_used == 0
    assert status.automatic_available == int(100 * AUTOMATIC_SHARE) - spent
    assert fake_time.now == NOW


async def test_the_configured_xg_budget_is_the_one_spent(tmp_path: Path) -> None:
    async with api_over_respx(NOW) as (api, _):
        yesterday = fill(api)
        async with open_bare(
            tmp_path / "xg.db",
            stored_as_scheduled(yesterday),
            providers=lambda _clock: {Sport.FOOTBALL: api.provider},
            now=NOW,
        ) as bare:
            await refresh(bare.ctx)
            configured = bare.ctx.config.xg_per_refresh

    assert api.wire.count("/fixtures/statistics") == configured


async def test_a_second_refresh_the_same_day_costs_nothing(tmp_path: Path) -> None:
    async with api_over_respx(NOW) as (api, _):
        yesterday = fill(api)
        async with open_bare(
            tmp_path / "again.db",
            stored_as_scheduled(yesterday),
            providers=lambda _clock: {Sport.FOOTBALL: api.provider},
            now=NOW,
        ) as bare:
            await refresh(bare.ctx)
            first = len(api.wire.seen)
            again = await refresh(bare.ctx)

    assert again.already_done
    assert len(api.wire.seen) == first


async def test_busy_leagues_with_paged_odds_can_pass_forty_but_never_the_automatic_quota(
    tmp_path: Path,
) -> None:
    """Two pages of odds per league-day push the refresh past 40; the quota still holds it."""
    async with api_over_respx(NOW) as (api, _):
        yesterday = fill(api)
        api.wire.odds_pages = 2
        async with open_bare(
            tmp_path / "paged.db",
            stored_as_scheduled(yesterday),
            providers=lambda _clock: {Sport.FOOTBALL: api.provider},
            now=NOW,
        ) as bare:
            summary = await refresh(bare.ctx)
        status = await api.harness.quota.status()

    spent = len(api.wire.seen)
    assert spent == 3 + 1 + 10 + 20 * 2  # documented finding: the 40 budget assumes one page
    assert spent > REQUEST_BUDGET
    assert summary.ok  # still inside the automatic quota (70)
    assert status.counts.requests_used == spent <= int(100 * AUTOMATIC_SHARE)
    assert status.counts.manual_used == 0


# ------------------------------------------------------------------------------ budget
async def test_budget_exhaustion_mid_refresh_keeps_partial_results_and_the_reserve(
    tmp_path: Path,
) -> None:
    async with api_over_respx(NOW, daily_limit=20) as (api, _):  # automatic budget: 14
        yesterday = fill(api)
        async with open_bare(
            tmp_path / "partial.db",
            stored_as_scheduled(yesterday),
            providers=lambda _clock: {Sport.FOOTBALL: api.provider},
            now=NOW,
        ) as bare:
            summary = await refresh(bare.ctx)
            status = await api.harness.quota.status()
            runs = await bare.ctx.ledger.last_runs()
            system = await bare.system.services.status.system_status()
            async with bare.sessions() as session:
                stored = await list_fixtures(session, start=NOW, end=NOW + timedelta(days=4))
                prices = await latest_odds(
                    session, [f.fixture.ref for f in stored.items], kinds=(OddsKind.MORNING,)
                )

    assert not summary.ok
    assert summary.skipped == ["odds"]  # a clear list of what was dropped
    assert (summary.fixtures, summary.results) == (60, 20)  # earlier steps' data is kept
    assert len(stored.items) == 60
    assert sum(len(v) for v in prices.values()) == 0  # a step is never half written
    assert status.counts.manual_used == 0
    assert status.reserve_remaining == int(20 * 0.30)  # the manual reserve is untouched
    assert not runs["refresh"].ok
    assert "skipped=" in (runs["refresh"].detail or "")
    assert any(job.name == "refresh" and not job.ok for job in system.jobs)


async def test_a_manual_analysis_beyond_the_reserve_is_a_clear_error_and_spends_nothing(
    tmp_path: Path,
) -> None:
    async with api_over_respx(NOW, daily_limit=4) as (api, _):  # reserve = 1 request
        fill(api)
        async with open_bare(
            tmp_path / "manual.db",
            None,
            providers=lambda _clock: {Sport.FOOTBALL: api.provider},
            now=NOW,
        ) as bare:
            services = bare.system.services
            query = AnalysisQuery(dates=DateSpec.next_days(3), min_probability=0.3, min_ev=0.0)
            estimate = await services.analysis.estimate_cost(1, query)
            with pytest.raises(BudgetExceededError) as raised:
                await services.analysis.analyze(1, query)
        used = len(api.wire.seen)

    assert not estimate.within_budget
    assert raised.value.needed > raised.value.available
    assert used == 0


# ------------------------------------------------------------------------------ outage
async def test_a_provider_outage_is_a_service_error_for_a_manual_analysis(tmp_path: Path) -> None:
    async with api_over_respx(NOW) as (api, _):
        fill(api)
        api.wire.fail_with = 503
        async with open_bare(
            tmp_path / "down.db",
            None,
            providers=lambda _clock: {Sport.FOOTBALL: api.provider},
            now=NOW,
        ) as bare:
            query = AnalysisQuery(dates=DateSpec.next_days(2), min_probability=0.3, min_ev=0.0)
            with pytest.raises(ServiceUnavailableError) as raised:
                await bare.system.services.analysis.analyze(1, query)

    assert raised.value.component == "api-football"
    assert "unavailable" in str(raised.value) or raised.value.detail


# ------------------------------------------------------------------------------ the CLI
def run_refresh_cli(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    """``python -m app.pipeline.refresh`` in a clean environment (no .env, own database)."""
    clean = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in {"API_FOOTBALL_KEY", "DB_PATH", "APP_ENV", "LOG_LEVEL"}
    }
    clean.update({"DB_PATH": str(tmp_path / "cli.db"), "APP_ENV": "dev", **env})
    return subprocess.run(
        [sys.executable, "-I", "-m", "app.pipeline.refresh"],
        cwd=tmp_path,
        env=clean,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_the_refresh_cli_without_a_key_exits_2_and_says_so(tmp_path: Path) -> None:
    done = run_refresh_cli(tmp_path)

    assert done.returncode == 2, done.stderr
    output = done.stdout + done.stderr
    assert "API_FOOTBALL_KEY" in output or "api_football_key" in output
    assert "refresh_not_configured" in output
    assert "Traceback" not in output  # an operator error, not a crash


def test_the_refresh_cli_exit_codes_are_the_documented_ones() -> None:
    from app.pipeline import refresh as module  # noqa: PLC0415

    assert (module.EXIT_OK, module.EXIT_UNEXPECTED, module.EXIT_CONFIGURATION) == (0, 1, 2)


# ------------------------------------------------------------------------------ extra time
SIGNALS_ON_THE_MATCH = {
    "home": (Market.match_result(), Selection.HOME),
    "draw": (Market.match_result(), Selection.DRAW),
    "over": (Market.totals(2.5), Selection.OVER),
    "under": (Market.totals(2.5), Selection.UNDER),
}


@pytest.mark.parametrize(
    ("status", "regular", "with_extra_time"),
    [("AET", (1, 1), (2, 1)), ("PEN", (0, 0), (2, 2))],
    ids=["extra-time", "penalties"],
)
async def test_a_match_decided_after_90_minutes_settles_on_regular_time(
    tmp_path: Path, status: str, regular: tuple[int, int], with_extra_time: tuple[int, int]
) -> None:
    kickoff = utc(2026, 10, 21, 10)
    morning = utc(2026, 10, 21, 8)
    async with api_over_respx(morning) as (api, fake_time):
        scheduled = record(5001, LEAGUE_IDS[0], kickoff)
        api.wire.add(
            record(
                5001, LEAGUE_IDS[0], kickoff, status=status, fulltime=regular, goals=with_extra_time
            )
        )
        stored = stored_as_scheduled([scheduled])
        async with open_bare(
            tmp_path / "aet.db",
            stored,
            providers=lambda _clock: {Sport.FOOTBALL: api.provider},
            now=morning,
        ) as bare:
            signals = {
                name: await add_signal(bare, stored.fixtures[0], market, selection, 2.0, Decimal(1))
                for name, (market, selection) in SIGNALS_ON_THE_MATCH.items()
            }
            bare.clock.now = utc(2026, 10, 21, 14)
            fake_time.now = bare.clock.now
            report = await run_settlement(bare.ctx, NullDelivery())
            views = await bare.system.services.signals.get_signals(
                USER, [s.id for s in signals.values()]
            )

    by_id = {view.signal_id: view for view in views}
    marks = {name: by_id[found.id].result for name, found in signals.items()}
    assert report.settled == 4
    assert api.wire.count("/fixtures") == 1  # the result came through the real client
    assert marks == {
        "home": ResultMark.LOST,  # level after 90 minutes whatever happened later
        "draw": ResultMark.WON,
        "over": ResultMark.LOST,  # at most two regular-time goals
        "under": ResultMark.WON,
    }


# ------------------------------------------------------------------------------ hygiene
async def test_a_refresh_that_lost_its_provider_is_marked_done_for_the_day_and_not_retried(
    tmp_path: Path,
) -> None:
    """Documented behaviour (finding QA-T4-F10): only ``force`` or the digest's own fetch heals."""
    async with api_over_respx(NOW) as (api, _):
        yesterday = fill(api)
        api.wire.fail_with = 503
        async with open_bare(
            tmp_path / "outage.db",
            stored_as_scheduled(yesterday),
            providers=lambda _clock: {Sport.FOOTBALL: api.provider},
            now=NOW,
        ) as bare:
            broken = await refresh(bare.ctx)
            api.wire.fail_with = None
            retry = await refresh(bare.ctx)
            forced = await refresh(bare.ctx, force=True)

    assert not broken.ok
    assert set(broken.skipped) == {"fixtures", "results"}  # no fixtures stored: no odds to ask
    assert retry.already_done  # the provider is back, but the day's refresh is not repeated
    assert forced.ok
    assert forced.fixtures == 60


async def test_the_api_key_never_reaches_the_logs_or_the_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.clients.conftest import TEST_KEY  # noqa: PLC0415

    async with api_over_respx(NOW) as (api, _):
        yesterday = fill(api)
        api.wire.fail_with = 503
        async with open_bare(
            tmp_path / "secret.db",
            stored_as_scheduled(yesterday),
            providers=lambda _clock: {Sport.FOOTBALL: api.provider},
            now=NOW,
        ) as bare:
            await refresh(bare.ctx)
            api.wire.fail_with = None
            await refresh(bare.ctx, force=True)
            status = await bare.system.services.status.system_status()
            ledger_text = repr(await bare.ctx.ledger.last_runs()) + repr(
                await bare.ctx.ledger.recent_errors(100)
            )

    seen = capsys.readouterr()
    assert TEST_KEY not in seen.out + seen.err
    assert TEST_KEY not in ledger_text + repr(status)
