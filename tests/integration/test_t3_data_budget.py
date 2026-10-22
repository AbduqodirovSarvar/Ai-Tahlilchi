"""T3 QA: F2 acceptance "one full refresh of 10 leagues <= 40 requests", and the probe script.

The refresh is simulated end to end (adapter -> client -> respx) and every request is counted
at the HTTP layer, then compared with the pure ``estimate_requests`` arithmetic.
"""

import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from functools import partial
from itertools import pairwise
from pathlib import Path

import httpx
import pytest
import respx

from app.clients.api_football import API_FOOTBALL_CONFIG, create_api_football_client
from app.clients.cost import FetchPlan, estimate_requests
from app.clients.quota import RequestPurpose
from app.config import get_settings
from app.domain import ExternalRef, OddsKind
from app.sports.base import FixtureQuery
from app.sports.football import FootballAdapter
from app.sports.football_leagues import DEFAULT_LEAGUES, season_for, seasons_between
from tests.clients.conftest import BASE_URL, TEST_KEY, Harness, build_harness, load
from tests.clients.conftest import FakeTime as FakeTime  # noqa: PLC0414 - fixture dependency
from tests.integration.history_helpers import REPO_ROOT

TODAY = date(2024, 10, 26)
SEASON = season_for(TODAY)
LEAGUE_IDS = [league.api_id for league in DEFAULT_LEAGUES]
FIXTURES_PER_LEAGUE = 2
BUDGET = 40


def fixture_record(
    fixture_id: int, league_id: int, day: date, *, status: str = "NS", goals: int | None = None
) -> dict[str, object]:
    kickoff = datetime(day.year, day.month, day.day, 15, 0, tzinfo=UTC).isoformat()
    score = {"home": goals, "away": goals}
    return {
        "fixture": {"id": fixture_id, "date": kickoff, "status": {"short": status}},
        "league": {"id": league_id, "name": f"League {league_id}", "season": SEASON},
        "teams": {
            "home": {"id": fixture_id * 10 + 1, "name": f"Home {fixture_id}"},
            "away": {"id": fixture_id * 10 + 2, "name": f"Away {fixture_id}"},
        },
        "goals": score,
        "score": {"fulltime": score, "halftime": {"home": None, "away": None}},
    }


def envelope(records: list[dict[str, object]], *, page: int = 1, pages: int = 1) -> httpx.Response:
    body = {
        "get": "x",
        "parameters": {},
        "errors": [],
        "results": len(records),
        "paging": {"current": page, "total": pages},
        "response": records,
    }
    return httpx.Response(200, json=body)


def today_fixtures() -> list[dict[str, object]]:
    return [
        fixture_record(league * 100 + index, league, TODAY)
        for league in LEAGUE_IDS
        for index in range(FIXTURES_PER_LEAGUE)
    ]


def yesterday_finished() -> list[dict[str, object]]:
    day = TODAY - timedelta(days=1)
    return [
        fixture_record(900_000 + index, LEAGUE_IDS[index % 10], day, status="FT", goals=1)
        for index in range(2 * len(LEAGUE_IDS))
    ]


def odds_record(fixture_id: int, league_id: int) -> dict[str, object]:
    return {
        "league": {"id": league_id, "season": SEASON},
        "fixture": {"id": fixture_id},
        "bookmakers": [
            {
                "id": 4,
                "name": "Pinnacle",
                "bets": [
                    {
                        "id": 1,
                        "name": "Match Winner",
                        "values": [
                            {"value": "Home", "odd": "2.00"},
                            {"value": "Draw", "odd": "3.50"},
                            {"value": "Away", "odd": "3.80"},
                        ],
                    }
                ],
            }
        ],
    }


def install_provider(router: respx.MockRouter, *, odds_pages: int) -> None:
    """A tiny API-Football: today's matches, history, results, odds (paged) and injuries."""

    def fixtures(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if "ids" in params:
            wanted = {int(part) for part in params["ids"].split("-")}
            return envelope(
                [r for r in yesterday_finished() if r["fixture"]["id"] in wanted]  # type: ignore[index]
            )
        if "date" in params:
            return envelope(today_fixtures())
        league = int(params["league"])
        return envelope([r for r in today_fixtures() if r["league"]["id"] == league])  # type: ignore[index]

    def odds(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        league = int(params["league"])
        page = int(params.get("page", "1"))
        records = [
            odds_record(r["fixture"]["id"], league)  # type: ignore[index]
            for r in today_fixtures()
            if r["league"]["id"] == league  # type: ignore[index]
        ]
        share = records[(page - 1) :: odds_pages] if odds_pages > 1 else records
        return envelope(share, page=page, pages=odds_pages)

    router.get("/fixtures").mock(side_effect=fixtures)
    router.get("/odds").mock(side_effect=odds)
    router.get("/injuries").mock(return_value=envelope([]))
    router.get("/fixtures/statistics").mock(return_value=envelope([]))


async def full_refresh(adapter: FootballAdapter, *, injury_candidates: int) -> int:
    """One daily refresh as plan §9 lists it; returns how many fixtures were seen."""
    leagues = [league.api_ref for league in DEFAULT_LEAGUES]
    await adapter.refresh_history(leagues, SEASON)  # 10 league seasons
    fixtures = await adapter.fetch_fixtures(FixtureQuery(date_from=TODAY, date_to=TODAY))  # 1
    odds = await adapter.fetch_odds([f.ref for f in fixtures], kind=OddsKind.MORNING)
    assert odds, "odds must be mapped for the fixtures just seen"
    finished = [ExternalRef("api-football", str(r["fixture"]["id"])) for r in yesterday_finished()]  # type: ignore[index]
    await adapter.fetch_results(finished)  # ceil(20 / 20)
    for fixture in fixtures[:injury_candidates]:
        await adapter.fetch_injuries(fixture.ref)
    return len(fixtures)


@pytest.mark.parametrize(("odds_pages", "expected"), [(1, 27), (2, 37)])
async def test_a_full_refresh_of_ten_leagues_stays_within_forty_requests(
    router: respx.MockRouter, harness: Harness, odds_pages: int, expected: int
) -> None:
    install_provider(router, odds_pages=odds_pages)
    adapter = FootballAdapter(harness.football, clock=harness.time.utc)
    seen = await full_refresh(adapter, injury_candidates=5)
    assert seen == len(LEAGUE_IDS) * FIXTURES_PER_LEAGUE
    counted = router.calls.call_count
    plan = FetchPlan(
        league_seasons=10,
        days=1,
        odds_league_days=10,
        odds_pages_per_league_day=odds_pages,
        fixtures_by_id=len(yesterday_finished()),
        injury_fixtures=5,
    )
    assert counted == estimate_requests(plan) == expected
    assert counted <= BUDGET
    status = await harness.quota.status()
    assert status.counts.requests_used == counted  # the quota counter agrees with the wire
    assert status.automatic_available == 70 - counted  # automatic budget; reserve untouched
    assert status.counts.manual_used == 0


async def test_free_plan_pacing_never_exceeds_ten_requests_per_minute(
    router: respx.MockRouter, http_client: httpx.AsyncClient, fake_time: FakeTime
) -> None:
    """The production config (10/min, burst 1) spaces a whole refresh >= 6 s apart."""
    harness = build_harness(fake_time, http_client, config=API_FOOTBALL_CONFIG)
    install_provider(router, odds_pages=1)
    stamps: list[float] = []

    async def stamp(_request: httpx.Request) -> None:
        stamps.append(fake_time.seconds)

    http_client.event_hooks = {"request": [stamp], "response": []}
    adapter = FootballAdapter(harness.football, clock=harness.time.utc)
    await full_refresh(adapter, injury_candidates=5)
    assert len(stamps) == 27
    gaps = [later - earlier for earlier, later in pairwise(stamps)]
    assert min(gaps) >= 6.0 - 1e-9
    for index, start in enumerate(stamps):
        in_window = [s for s in stamps[index:] if s < start + 60.0]
        assert len(in_window) <= 10
    # Wall-clock cost of the refresh on the free plan (background job, not a user wait).
    assert stamps[-1] - stamps[0] == pytest.approx(26 * 6.0)


async def test_an_immediate_second_refresh_is_served_from_cache(
    router: respx.MockRouter, harness: Harness
) -> None:
    install_provider(router, odds_pages=1)
    adapter = FootballAdapter(harness.football, clock=harness.time.utc)
    await full_refresh(adapter, injury_candidates=5)
    first = router.calls.call_count
    await full_refresh(adapter, injury_candidates=5)
    assert router.calls.call_count == first  # same data is not requested twice in 10 minutes
    harness.time.seconds += 601
    await full_refresh(adapter, injury_candidates=5)
    assert router.calls.call_count == 2 * first


async def test_the_refresh_only_ever_charges_the_automatic_budget(
    router: respx.MockRouter, harness: Harness
) -> None:
    install_provider(router, odds_pages=1)
    adapter = FootballAdapter(
        harness.football, clock=harness.time.utc, purpose=RequestPurpose.AUTOMATIC
    )
    await full_refresh(adapter, injury_candidates=3)
    assert (await harness.quota.status()).counts.manual_used == 0


def test_estimate_matches_the_documented_budget_table() -> None:
    """Plan §9: 10 + 1 + 10..15 + 3..5 + 1..2 = 25..43 (lineups are priced separately)."""
    low = FetchPlan(
        league_seasons=10, days=1, odds_league_days=10, injury_fixtures=3, fixtures_by_id=1
    )
    high = FetchPlan(
        league_seasons=10,
        days=1,
        odds_league_days=15,
        injury_fixtures=5,
        fixtures_by_id=40,
        lineup_fixtures=10,
    )
    assert estimate_requests(low) == 25
    assert estimate_requests(high) == 43


def test_expected_goals_for_every_new_result_would_break_the_budget() -> None:
    """Finding QA-T3-B1: plan §9 has no xG line, yet each xG costs one request.

    Twenty newly finished matches (two per league) already push the daily refresh far over
    forty requests; the pipeline must cap xG fetching by the remaining automatic quota.
    """
    base = FetchPlan(
        league_seasons=10, days=1, odds_league_days=10, fixtures_by_id=20, injury_fixtures=5
    )
    assert estimate_requests(base) <= BUDGET
    with_xg = FetchPlan(
        league_seasons=10,
        days=1,
        odds_league_days=10,
        fixtures_by_id=20,
        injury_fixtures=5,
        statistics_fixtures=20,
    )
    assert estimate_requests(with_xg) == estimate_requests(base) + 20 > BUDGET
    capped = replace(with_xg, max_xg_requests=8)  # the fix: xG is capped and budget-aware
    assert estimate_requests(capped) == estimate_requests(base) + 8 <= BUDGET


async def test_expected_goals_are_one_request_per_finished_fixture(
    router: respx.MockRouter, harness: Harness
) -> None:
    install_provider(router, odds_pages=1)
    adapter = FootballAdapter(harness.football, clock=harness.time.utc)
    finished = [ExternalRef("api-football", str(r["fixture"]["id"])) for r in yesterday_finished()]  # type: ignore[index]
    results = await adapter.fetch_results(finished)
    before = router.calls.call_count
    await adapter.with_expected_goals(results, max_requests=len(results))
    assert router.calls.call_count - before == len(results)
    before = router.calls.call_count
    await adapter.with_expected_goals(results)  # default cap: xG never swamps the budget
    assert router.calls.call_count - before < len(results)


@pytest.mark.parametrize(
    ("first", "last", "expected"),
    [
        (date(2024, 10, 26), date(2024, 10, 26), 1),  # one day: one request for every league
        (date(2024, 10, 20), date(2024, 10, 26), 7),  # a week: one per day
        (date(2024, 9, 1), date(2024, 10, 26), 10),  # long window: one per league season
        (date(2024, 6, 20), date(2024, 7, 20), 20),  # across 1 July: two seasons per league
    ],
)
async def test_sync_window_cost_is_bounded_by_league_seasons(
    router: respx.MockRouter, harness: Harness, first: date, last: date, expected: int
) -> None:
    router.get("/fixtures").mock(return_value=envelope([]))
    adapter = FootballAdapter(harness.football, clock=harness.time.utc)
    await adapter.fetch_fixtures(FixtureQuery(date_from=first, date_to=last))
    assert router.calls.call_count == expected
    assert expected <= len(DEFAULT_LEAGUES) * len(seasons_between(first, last))


# --------------------------------------------------------------------------- probe script


def run_probe_subprocess(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    clean = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("API_FOOTBALL", "TELEGRAM", "GEMINI", "ODDS_API"))
    }
    clean.update(
        HTTPS_PROXY="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9", NO_PROXY="", **env
    )
    return subprocess.run(  # noqa: S603 - fixed argv, repository script
        [sys.executable, str(REPO_ROOT / "scripts" / "probe_api.py"), "--no-record"],
        cwd=tmp_path,  # no .env here
        env=clean,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize("key", [None, "", "   "])
def test_probe_without_a_key_exits_2_and_sends_nothing(tmp_path: Path, key: str | None) -> None:
    extra = {} if key is None else {"API_FOOTBALL_KEY": key}
    done = run_probe_subprocess(tmp_path, **extra)
    output = done.stdout + done.stderr
    assert done.returncode == 2, output
    assert "API_FOOTBALL_KEY" in output
    # Exit code 1 would mean it tried to reach the (deliberately dead) proxy.
    assert not (tmp_path / "docs").exists()


def test_probe_full_run_spends_at_most_twenty_five_requests_and_leaks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = importlib.util.spec_from_file_location(
        "qa_probe", REPO_ROOT / "scripts" / "probe_api.py"
    )
    assert spec is not None
    assert spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    fast = replace(API_FOOTBALL_CONFIG, requests_per_minute=60_000, burst=100)
    monkeypatch.setattr(
        script, "create_api_football_client", partial(create_api_football_client, config=fast)
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("API_FOOTBALL_KEY", TEST_KEY)
    get_settings.cache_clear()
    report, record_dir = tmp_path / "report.md", tmp_path / "recorded"
    try:
        with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
            router.get("/status").respond(json=load("status.json"))
            router.get("/fixtures/statistics").respond(json=load("statistics_fixture_1208021.json"))
            router.get("/fixtures/lineups").respond(json=load("lineups_fixture_1208100.json"))
            router.get("/injuries").respond(json=load("injuries_fixture_1208100.json"))
            router.get("/odds").respond(json=load("odds_fixture_1208100.json"))
            router.get("/fixtures").respond(json=load("fixtures_league_39_2024.json"))
            code = script.main(
                ["--report", str(report), "--record-dir", str(record_dir)],
                now=datetime(2024, 10, 20, 9, 0, tzinfo=UTC),
            )
            calls = router.calls
            hosts = {call.request.url.host for call in calls}
            keys = {call.request.headers["x-apisports-key"] for call in calls}
    finally:
        get_settings.cache_clear()
    assert code == 0
    assert len(calls) <= 1 + 2 * len(DEFAULT_LEAGUES) + 4
    assert hosts == {"v3.football.api-sports.io"}
    assert keys == {TEST_KEY}  # the key only travels in the header
    produced = [report, *record_dir.glob("*.json")]
    texts = [path.read_text(encoding="utf-8") for path in produced]
    captured = capsys.readouterr()
    for blob in [*texts, captured.out, captured.err]:
        assert TEST_KEY not in blob
    status_text = json.dumps(json.loads((record_dir / "status_all.json").read_text("utf-8")))
    for private in ("email", "firstname", "lastname"):
        assert private not in status_text


@pytest.mark.xfail(
    strict=True,
    reason="QA-T3-A1: F2 acceptance names `python -m app.pipeline.refresh`; the module does not "
    "exist yet (pipeline wave); only the adapter-level refresh is verified here",
)
def test_the_refresh_entry_point_named_by_the_acceptance_criterion_exists() -> None:
    assert importlib.util.find_spec("app.pipeline.refresh") is not None
