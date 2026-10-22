"""T3 fix round: capped/attributed xG, honest odds capture times, null prices."""

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from app.clients.api_football_models import OddsItem
from app.clients.cost import FetchPlan, estimate_requests, xg_requests
from app.clients.quota import RequestPurpose
from app.domain import ExternalRef, FixtureResult, OddsKind
from app.sports.football import (
    DEFAULT_MAX_XG_REQUESTS,
    XG_AUTOMATIC_FLOOR,
    FootballAdapter,
)
from app.sports.football_mapping import map_odds
from tests.clients.conftest import BASE_URL, TEST_KEY, FakeTime, Harness, build_harness, load

FIXTURES_IDS = "fixtures_ids.json"
UNITED_FULHAM = 1208021  # home 33 (Manchester United), away 36 (Fulham)


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


def _of(results: list[FixtureResult]) -> FixtureResult:
    return next(r for r in results if r.fixture == fx(UNITED_FULHAM))


def statistics(teams_xg: list[tuple[int, str]]) -> dict[str, object]:
    return {
        "get": "fixtures/statistics",
        "errors": [],
        "paging": {"current": 1, "total": 1},
        "response": [
            {
                "team": {"id": team_id, "name": f"Team {team_id}"},
                "statistics": [{"type": "expected_goals", "value": xg}],
            }
            for team_id, xg in teams_xg
        ],
    }


def finished_item(fixture_id: int) -> dict[str, object]:
    score = {"home": 1, "away": 0}
    return {
        "fixture": {
            "id": fixture_id,
            "date": "2024-10-25T19:00:00+00:00",
            "status": {"short": "FT"},
        },
        "league": {"id": 39, "name": "Premier League", "season": 2024},
        "teams": {
            "home": {"id": fixture_id * 10 + 1, "name": "H"},
            "away": {"id": fixture_id * 10 + 2, "name": "A"},
        },
        "goals": score,
        "score": {"fulltime": score, "halftime": {"home": 0, "away": 0}},
    }


def envelope(records: list[dict[str, object]]) -> httpx.Response:
    body = {"errors": [], "paging": {"current": 1, "total": 1}, "response": records}
    return httpx.Response(200, json=body)


# --------------------------------------------------------------------------- xG attribution


async def test_xg_is_matched_by_team_id_not_by_list_order(
    adapter: FootballAdapter, router: respx.MockRouter
) -> None:
    router.get("/fixtures").respond(json=load(FIXTURES_IDS))
    # The provider lists the away team first this time.
    router.get("/fixtures/statistics").respond(json=statistics([(36, "0.48"), (33, "1.73")]))
    results = await adapter.fetch_results([fx(UNITED_FULHAM)])
    enriched = {r.fixture: r for r in await adapter.with_expected_goals(results)}[fx(UNITED_FULHAM)]
    assert (enriched.home_xg, enriched.away_xg) == (1.73, 0.48)


async def test_xg_of_unrelated_teams_is_ignored(
    adapter: FootballAdapter, router: respx.MockRouter
) -> None:
    router.get("/fixtures").respond(json=load(FIXTURES_IDS))
    router.get("/fixtures/statistics").respond(json=statistics([(1, "1.0"), (2, "2.0")]))
    results = await adapter.fetch_results([fx(UNITED_FULHAM)])
    enriched = {r.fixture: r for r in await adapter.with_expected_goals(results)}[fx(UNITED_FULHAM)]
    assert (enriched.home_xg, enriched.away_xg) == (None, None)


async def test_xg_for_a_fixture_the_adapter_never_saw_costs_nothing(
    adapter: FootballAdapter, router: respx.MockRouter
) -> None:
    router.get("/fixtures").respond(json=load(FIXTURES_IDS))
    stats = router.get("/fixtures/statistics").respond(json=statistics([(33, "1.0"), (36, "2.0")]))
    results = [
        r
        for r in await adapter.fetch_results([fx(UNITED_FULHAM)])
        if r.fixture == fx(UNITED_FULHAM)
    ]
    fresh = adapter.for_purpose(RequestPurpose.AUTOMATIC)  # shares what was seen
    assert _of(await fresh.with_expected_goals(results)).home_xg == 1.0
    stranger = FootballAdapter(adapter._client, clock=adapter._clock)
    assert _of(await stranger.with_expected_goals(results)).home_xg is None
    assert stats.call_count == 1


# --------------------------------------------------------------------------- xG budget


async def seen_results(
    adapter: FootballAdapter, router: respx.MockRouter, count: int
) -> list[ExternalRef]:
    records = [finished_item(900_000 + index) for index in range(count)]
    router.get("/fixtures").mock(return_value=envelope(records))
    refs = [fx(900_000 + index) for index in range(count)]
    results = await adapter.fetch_results(refs)
    assert len(results) == count
    return refs


async def test_xg_requests_are_capped_per_call(
    adapter: FootballAdapter, router: respx.MockRouter
) -> None:
    refs = await seen_results(adapter, router, 20)
    router.get("/fixtures/statistics").mock(return_value=envelope([]))
    results = await adapter.fetch_results(refs)
    before = router.calls.call_count
    await adapter.with_expected_goals(results, max_requests=3)
    assert router.calls.call_count - before == 3
    before = router.calls.call_count
    out = await adapter.with_expected_goals(results)  # default cap 8; three are cached already
    assert router.calls.call_count - before == DEFAULT_MAX_XG_REQUESTS - 3
    assert len(out) == 20  # the rest keep their results, only without xG
    before = router.calls.call_count
    await adapter.with_expected_goals(results, max_requests=0)
    assert router.calls.call_count == before


async def test_xg_stops_when_the_automatic_quota_is_low(
    adapter: FootballAdapter, harness: Harness, router: respx.MockRouter
) -> None:
    refs = await seen_results(adapter, router, 5)
    router.get("/fixtures/statistics").mock(return_value=envelope([]))
    results = await adapter.fetch_results(refs)
    spend = 70 - XG_AUTOMATIC_FLOOR - 2  # leaves two xG requests above the floor
    await harness.quota.charge(RequestPurpose.AUTOMATIC, spend - (await _used(harness)))
    before = router.calls.call_count
    await adapter.with_expected_goals(results, max_requests=10)
    assert router.calls.call_count - before == 2
    await harness.quota.charge(RequestPurpose.AUTOMATIC, 5)
    before = router.calls.call_count
    await adapter.with_expected_goals(results, max_requests=10)
    assert router.calls.call_count == before


async def _used(harness: Harness) -> int:
    return (await harness.quota.status()).counts.requests_used


async def test_manual_xg_is_limited_only_by_the_cap_and_the_whole_quota(
    harness: Harness, router: respx.MockRouter
) -> None:
    adapter = FootballAdapter(
        harness.football, clock=harness.time.utc, purpose=RequestPurpose.MANUAL
    )
    refs = await seen_results(adapter, router, 4)
    router.get("/fixtures/statistics").mock(return_value=envelope([]))
    await harness.quota.charge(RequestPurpose.MANUAL, 100 - 2 - await _used(harness))
    before = router.calls.call_count
    await adapter.with_expected_goals(await adapter.fetch_results(refs), max_requests=10)
    assert router.calls.call_count - before == 2  # only what is left of the whole quota


async def test_a_refresh_with_twenty_new_results_stays_within_forty_requests(
    adapter: FootballAdapter, harness: Harness, router: respx.MockRouter
) -> None:
    """Plan §9: core refresh 27 requests + capped xG for 20 new results <= 40."""
    router.get("/fixtures/statistics").mock(return_value=envelope([]))
    core = 27
    await harness.quota.charge(RequestPurpose.AUTOMATIC, core - 1)  # fetch_results costs 1
    refs = await seen_results(adapter, router, 20)
    results = await adapter.fetch_results(refs)
    await adapter.with_expected_goals(results)
    assert (await _used(harness)) <= 40
    plan = FetchPlan(statistics_fixtures=20, max_xg_requests=DEFAULT_MAX_XG_REQUESTS)
    assert xg_requests(plan) == estimate_requests(plan) == DEFAULT_MAX_XG_REQUESTS
    assert core + estimate_requests(plan) <= 40


def test_estimate_without_a_cap_counts_every_fixture() -> None:
    assert estimate_requests(FetchPlan(statistics_fixtures=20)) == 20
    assert estimate_requests(FetchPlan(statistics_fixtures=3, max_xg_requests=8)) == 3
    with pytest.raises(ValueError, match="max_xg_requests"):
        FetchPlan(max_xg_requests=-1)


# --------------------------------------------------------------------------- odds capture time


async def test_cached_odds_keep_the_time_they_were_really_taken(
    adapter: FootballAdapter, harness: Harness, router: respx.MockRouter
) -> None:
    route = router.get("/odds").respond(json=load("odds_fixture_1208100.json"))
    first = await adapter.fetch_odds([fx(1208100)], kind=OddsKind.MORNING)
    taken = harness.time.now
    harness.time.now += timedelta(minutes=7)
    harness.time.seconds += 7 * 60
    second = await adapter.fetch_odds([fx(1208100)], kind=OddsKind.PREMATCH)
    assert route.call_count == 1  # served from cache
    assert {snap.taken_at for snap in first} == {taken}
    assert {snap.taken_at for snap in second} == {taken}
    assert {snap.kind for snap in second} == {OddsKind.PREMATCH}


async def test_closing_odds_are_never_answered_from_the_cache(
    adapter: FootballAdapter, harness: Harness, router: respx.MockRouter
) -> None:
    route = router.get("/odds").respond(json=load("odds_fixture_1208100.json"))
    await adapter.fetch_odds([fx(1208100)], kind=OddsKind.PREMATCH)
    harness.time.now += timedelta(minutes=1)
    closing = await adapter.fetch_odds([fx(1208100)], kind=OddsKind.CLOSING)
    assert route.call_count == 2
    assert {snap.taken_at for snap in closing} == {harness.time.now}
    await adapter.fetch_odds([fx(1208100)], kind=OddsKind.CLOSING)
    assert route.call_count == 3  # and not cached afterwards either


# --------------------------------------------------------------------------- null price


def test_a_null_price_drops_only_that_value() -> None:
    item = OddsItem.model_validate(
        {
            "league": {"id": 39, "season": 2024},
            "fixture": {"id": 1},
            "bookmakers": [
                {
                    "id": 4,
                    "name": "Pinnacle",
                    "bets": [
                        {
                            "id": 1,
                            "name": "Match Winner",
                            "values": [
                                {"value": "Home", "odd": None},
                                {"value": "Draw", "odd": "3.50"},
                                {"value": "Away"},
                            ],
                        }
                    ],
                }
            ],
        }
    )
    taken = datetime(2024, 10, 26, 8, 0, tzinfo=UTC)
    snapshots, stats = map_odds([item], kind=OddsKind.MORNING, taken_at=taken)
    assert [(snap.selection.value, snap.odds) for snap in snapshots] == [("draw", 3.5)]
    assert stats.invalid_prices == 2
