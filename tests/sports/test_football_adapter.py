"""FootballAdapter over respx-mocked API-Football: conformance, request strategy, mapping."""

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime

import httpx
import pytest
import respx

from app.clients.quota import RequestPurpose
from app.domain import ExternalRef, FixtureStatus, MarketType, OddsKind, ResultStatus, Sport
from app.sports.base import FixtureQuery, SportAdapter
from app.sports.football import FootballAdapter
from app.sports.football_leagues import LeagueCatalog, TrackedLeague
from app.sports.team_matching import IdentityMap, match_teams
from tests.clients.conftest import BASE_URL, TEST_KEY, FakeTime, Harness, build_harness, load
from tests.sports.test_team_matching import HISTORY

EPL = ExternalRef("api-football", "39")


def fx(fixture_id: int) -> ExternalRef:
    return ExternalRef("api-football", str(fixture_id))


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
async def harness(router: respx.MockRouter) -> AsyncIterator[Harness]:  # noqa: ARG001
    async with httpx.AsyncClient(
        base_url=BASE_URL, headers={"x-apisports-key": TEST_KEY}
    ) as client:
        yield build_harness(FakeTime(), client)


@pytest.fixture
def adapter(harness: Harness) -> FootballAdapter:
    return FootballAdapter(harness.football, clock=harness.time.utc)


def test_conforms_to_sport_adapter(adapter: FootballAdapter) -> None:
    conforming: SportAdapter = adapter
    assert conforming.sport is Sport.FOOTBALL
    assert conforming.supported_markets() == frozenset(MarketType)


async def test_single_day_for_all_leagues_uses_one_date_request(
    adapter: FootballAdapter, harness: Harness, router: respx.MockRouter
) -> None:
    route = router.get("/fixtures").respond(json=load("fixtures_date_2024-10-26.json"))
    fixtures = await adapter.fetch_fixtures(
        FixtureQuery(date_from=date(2024, 10, 26), date_to=date(2024, 10, 26))
    )
    assert route.call_count == 1
    assert route.calls.last.request.url.params["date"] == "2024-10-26"
    # Untracked league 999 and the FA Cup (45) are filtered out.
    assert {f.league.ref for f in fixtures} == {EPL}
    assert [f.ref for f in fixtures] == [fx(1208100), fx(1208101), fx(1208105), fx(1208106)]
    statuses = {f.ref.external_id: f.status for f in fixtures}
    assert statuses["1208101"] is FixtureStatus.LIVE
    assert (await harness.quota.status()).counts.requests_used == 1


async def test_long_window_for_one_league_uses_league_request(
    adapter: FootballAdapter, router: respx.MockRouter
) -> None:
    route = router.get("/fixtures").respond(json=load("fixtures_league_39_2024.json"))
    fixtures = await adapter.fetch_fixtures(
        FixtureQuery(
            date_from=date(2024, 8, 16),
            date_to=date(2024, 8, 18),
            leagues=frozenset({ExternalRef("football-data", "E0")}),
        )
    )
    params = route.calls.last.request.url.params
    assert (params["league"], params["season"], params["from"], params["to"]) == (
        "39",
        "2024",
        "2024-08-16",
        "2024-08-18",
    )
    # The 2025 fixture is outside the window; the malformed record is skipped.
    assert len(fixtures) == 7
    assert fixtures[-1].kickoff == datetime(2024, 8, 18, 1, 30, tzinfo=UTC)


async def test_untracked_league_is_skipped(
    adapter: FootballAdapter, router: respx.MockRouter
) -> None:
    route = router.get("/fixtures").respond(json=load("fixtures_empty.json"))
    fixtures = await adapter.fetch_fixtures(
        FixtureQuery(
            date_from=date(2024, 8, 16),
            date_to=date(2024, 8, 16),
            leagues=frozenset({ExternalRef("api-football", "999")}),
        )
    )
    assert fixtures == []
    assert route.call_count == 0


async def test_team_scope_queries_per_team_and_season(
    adapter: FootballAdapter, router: respx.MockRouter
) -> None:
    route = router.get("/fixtures").respond(json=load("fixtures_ids.json"))
    query = FixtureQuery(
        date_from=date(2024, 6, 25),
        date_to=date(2024, 8, 20),
        teams=frozenset({ExternalRef("api-football", "33"), ExternalRef("football-data", "X")}),
    )
    fixtures = await adapter.fetch_fixtures(query)
    seasons = [call.request.url.params["season"] for call in route.calls]
    assert seasons == ["2023", "2024"]  # the window crosses 1 July
    assert {call.request.url.params["team"] for call in route.calls} == {"33"}
    assert [f.ref for f in fixtures] == [fx(1208021), fx(1208023), fx(1208024)]


async def test_leagues_and_teams_are_a_union_without_duplicates(
    adapter: FootballAdapter, router: respx.MockRouter
) -> None:
    router.get("/fixtures", params={"team": "33"}).respond(json=load("fixtures_ids.json"))
    router.get("/fixtures").respond(json=load("fixtures_league_39_2024.json"))
    fixtures = await adapter.fetch_fixtures(
        FixtureQuery(
            date_from=date(2024, 8, 16),
            date_to=date(2024, 8, 18),
            leagues=frozenset({EPL}),
            teams=frozenset({ExternalRef("api-football", "33")}),
        )
    )
    refs = [f.ref for f in fixtures]
    assert len(refs) == len(set(refs)) == 7


async def test_odds_for_seen_fixtures_of_one_league_day_use_paged_league_request(
    adapter: FootballAdapter, harness: Harness, router: respx.MockRouter
) -> None:
    router.get("/fixtures").respond(json=load("fixtures_date_2024-10-26.json"))
    page2 = router.get("/odds", params={"page": "2"}).respond(
        json=load("odds_league_39_2024-10-26_page2.json")
    )
    page1 = router.get("/odds").respond(json=load("odds_league_39_2024-10-26_page1.json"))
    await adapter.fetch_fixtures(
        FixtureQuery(date_from=date(2024, 10, 26), date_to=date(2024, 10, 26))
    )
    snapshots = await adapter.fetch_odds([fx(1208100), fx(1208105)], kind=OddsKind.MORNING)
    assert page1.call_count == page2.call_count == 1
    params = page1.calls.last.request.url.params
    assert (params["league"], params["season"], params["date"]) == ("39", "2024", "2024-10-26")
    assert {s.fixture for s in snapshots} == {fx(1208100), fx(1208105)}
    assert {s.bookmaker for s in snapshots} == {"pinnacle", "bet365"}
    assert {s.taken_at for s in snapshots} == {harness.time.now}
    # Same day as the clock: default 10-minute cache, so a refetch after 11 min costs again.
    harness.time.seconds += 11 * 60
    await adapter.fetch_odds([fx(1208100), fx(1208105)], kind=OddsKind.RECALC)
    assert page1.call_count == 2


async def test_future_day_odds_are_cached_for_six_hours(
    harness: Harness, router: respx.MockRouter
) -> None:
    adapter = FootballAdapter(harness.football, clock=lambda: datetime(2024, 10, 20, tzinfo=UTC))
    router.get("/fixtures").respond(json=load("fixtures_date_2024-10-26.json"))
    route = router.get("/odds").respond(json=load("odds_fixture_1208100.json"))
    await adapter.fetch_fixtures(
        FixtureQuery(date_from=date(2024, 10, 26), date_to=date(2024, 10, 26))
    )
    await adapter.fetch_odds([fx(1208100)], kind=OddsKind.MORNING)
    harness.time.seconds += 5 * 3600
    await adapter.fetch_odds([fx(1208100)], kind=OddsKind.MORNING)
    assert route.call_count == 1
    assert route.calls.last.request.url.params["fixture"] == "1208100"


async def test_odds_for_unknown_fixture_use_per_fixture_request(
    adapter: FootballAdapter, router: respx.MockRouter
) -> None:
    route = router.get("/odds").respond(json=load("odds_fixture_1208100.json"))
    snapshots = await adapter.fetch_odds(
        [fx(1208100), ExternalRef("football-data", "E0/2024/A/B")], kind=OddsKind.PREMATCH
    )
    assert route.call_count == 1
    assert snapshots
    assert {s.kind for s in snapshots} == {OddsKind.PREMATCH}


async def test_results_batch_ids_and_omit_unfinished(
    adapter: FootballAdapter, router: respx.MockRouter
) -> None:
    route = router.get("/fixtures").respond(json=load("fixtures_date_2024-10-26.json"))
    results = await adapter.fetch_results([fx(i) for i in range(1208100, 1208107)])
    assert route.call_count == 1
    assert route.calls.last.request.url.params["ids"] == "-".join(
        str(i) for i in range(1208100, 1208107)
    )
    by_ref = {r.fixture: r for r in results}
    assert fx(1208100) not in by_ref  # scheduled
    assert fx(1208101) not in by_ref  # live
    assert by_ref[fx(1208102)].status is ResultStatus.FINISHED
    assert by_ref[fx(1208106)].status is ResultStatus.ABANDONED


async def test_results_with_expected_goals(
    adapter: FootballAdapter, router: respx.MockRouter
) -> None:
    router.get("/fixtures").respond(json=load("fixtures_ids.json"))
    stats = router.get("/fixtures/statistics", params={"fixture": "1208021"}).respond(
        json=load("statistics_fixture_1208021.json")
    )
    router.get("/fixtures/statistics").respond(json=load("statistics_no_xg.json"))
    results = await adapter.fetch_results([fx(1208021), fx(1208023), fx(1208024)])
    enriched = {r.fixture: r for r in await adapter.with_expected_goals(results)}
    assert (enriched[fx(1208021)].home_xg, enriched[fx(1208021)].away_xg) == (1.73, 0.48)
    assert enriched[fx(1208023)].home_xg is None  # no usable xG
    assert enriched[fx(1208024)].status is ResultStatus.POSTPONED  # not requested
    assert stats.call_count == 1


async def test_refresh_history_returns_fixtures_with_results(
    adapter: FootballAdapter, router: respx.MockRouter
) -> None:
    route = router.get("/fixtures").respond(json=load("fixtures_league_39_2024.json"))
    batch = await adapter.refresh_history(
        [ExternalRef("football-data", "E0"), ExternalRef("api-football", "999")], 2024
    )
    assert route.call_count == 1
    assert route.calls.last.request.url.params["season"] == "2024"
    assert len(batch.fixtures) == 8
    assert {r.fixture for r in batch.results} == {
        fx(i) for i in (1208021, 1208022, 1208023, 1208024, 1208025, 1208026, 1208028)
    }
    assert batch.odds == ()


async def test_identity_map_links_live_fixtures_to_history_teams(
    harness: Harness, router: respx.MockRouter
) -> None:
    router.get("/fixtures", params={"team": "33"}).respond(json=load("fixtures_ids.json"))
    router.get("/fixtures").respond(json=load("fixtures_league_39_2024.json"))
    raw = FootballAdapter(harness.football, clock=harness.time.utc)
    batch = await raw.refresh_history([EPL], 2024)
    identity = IdentityMap.from_report(match_teams(batch.teams, HISTORY), LeagueCatalog())
    adapter = raw.with_identity(identity)
    fixtures = await adapter.fetch_fixtures(
        FixtureQuery(
            date_from=date(2024, 8, 16),
            date_to=date(2024, 8, 16),
            teams=frozenset({ExternalRef("football-data", "Man United")}),
        )
    )
    assert fixtures[0].home.ref == ExternalRef("football-data", "Man United")
    assert fixtures[0].league.ref == ExternalRef("football-data", "E0")


async def test_manual_adapter_charges_manual_quota(
    adapter: FootballAdapter, harness: Harness, router: respx.MockRouter
) -> None:
    router.get("/fixtures/lineups").respond(json=load("lineups_fixture_1208100.json"))
    router.get("/injuries").respond(json=load("injuries_fixture_1208100.json"))
    manual = adapter.for_purpose(RequestPurpose.MANUAL)
    lineups = await manual.fetch_lineups(fx(1208100))
    injuries = await manual.fetch_injuries(fx(1208100))
    assert len(lineups) == 2
    assert len(injuries) == 2
    counts = (await harness.quota.status()).counts
    assert (counts.requests_used, counts.manual_used) == (2, 2)
    assert await manual.fetch_lineups(ExternalRef("football-data", "x")) == []
    assert await manual.fetch_injuries(ExternalRef("api-football", "abc")) == []


async def test_bad_fixture_status_is_skipped(
    adapter: FootballAdapter, router: respx.MockRouter
) -> None:
    body = load("fixtures_date_2024-10-26.json")
    assert isinstance(body, dict)
    records = body["response"]
    assert isinstance(records, list)
    first = records[0]
    assert isinstance(first, dict)
    fixture = first["fixture"]
    assert isinstance(fixture, dict)
    fixture["status"] = {"short": "ZZZ"}
    router.get("/fixtures").respond(json=body)
    fixtures = await adapter.fetch_fixtures(
        FixtureQuery(date_from=date(2024, 10, 26), date_to=date(2024, 10, 26))
    )
    assert fx(1208100) not in {f.ref for f in fixtures}
    results = await adapter.fetch_results([fx(1208100)])
    assert results == [r for r in results if r.fixture != fx(1208100)]


async def test_remembered_fixtures_are_bounded(
    harness: Harness, router: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.sports.football.MAX_REMEMBERED_FIXTURES", 2)
    router.get("/fixtures").respond(json=load("fixtures_league_39_2024.json"))
    route = router.get("/odds").respond(json=load("fixtures_empty.json"))
    adapter = FootballAdapter(
        harness.football, catalog=LeagueCatalog([TrackedLeague(39, "E0")]), clock=harness.time.utc
    )
    await adapter.refresh_history([EPL], 2024)
    # Only the last two fixtures are remembered: the first one is fetched on its own.
    await adapter.fetch_odds([fx(1208021)], kind=OddsKind.MORNING)
    assert route.calls.last.request.url.params["fixture"] == "1208021"
