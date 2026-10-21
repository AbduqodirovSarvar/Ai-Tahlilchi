"""API-Football endpoint client over respx-recorded (synthetic) responses."""

from datetime import UTC, date, datetime

import httpx
import pytest
import respx
from pydantic import ValidationError

from app.clients.api_football import (
    MAX_IDS_PER_REQUEST,
    ApiFootballError,
    create_api_football_client,
)
from app.clients.api_football_models import Envelope, FixtureItem
from app.clients.quota import InMemoryUsageStore, RequestPurpose
from app.errors import ExternalServiceError
from tests.clients.conftest import TEST_KEY, Harness, load

AUTO = RequestPurpose.AUTOMATIC


async def test_fixtures_by_league_skips_malformed_records(
    harness: Harness, router: respx.MockRouter
) -> None:
    route = router.get("/fixtures", params={"league": "39", "season": "2024"}).respond(
        json=load("fixtures_league_39_2024.json")
    )
    items = await harness.football.fixtures_by_league(39, 2024, purpose=AUTO)
    assert len(items) == 8  # 9 records, one without teams
    assert all(isinstance(item, FixtureItem) for item in items)
    assert items[0].teams.home.name == "Manchester United"
    assert items[7].fixture.date == datetime(2024, 8, 18, 1, 30, tzinfo=UTC)
    assert "from" not in route.calls.last.request.url.params


async def test_fixtures_by_league_with_date_range(
    harness: Harness, router: respx.MockRouter
) -> None:
    route = router.get("/fixtures").respond(json=load("fixtures_empty.json"))
    await harness.football.fixtures_by_league(
        39, 2024, purpose=AUTO, date_from=date(2024, 10, 1), date_to=date(2024, 10, 7)
    )
    params = route.calls.last.request.url.params
    assert (params["from"], params["to"], params["season"]) == ("2024-10-01", "2024-10-07", "2024")


async def test_fixtures_by_date_and_by_team(harness: Harness, router: respx.MockRouter) -> None:
    route = router.get("/fixtures").respond(json=load("fixtures_date_2024-10-26.json"))
    items = await harness.football.fixtures_by_date(date(2024, 10, 26), purpose=AUTO)
    assert len(items) == 7
    await harness.football.fixtures_by_team(
        33, 2024, date_from=date(2024, 10, 1), date_to=date(2024, 10, 31), purpose=AUTO
    )
    params = route.calls.last.request.url.params
    assert (params["team"], params["season"], params["from"]) == ("33", "2024", "2024-10-01")


async def test_fixtures_by_ids_batches_twenty_per_request(
    harness: Harness, router: respx.MockRouter
) -> None:
    route = router.get("/fixtures").respond(json=load("fixtures_ids.json"))
    ids = [*range(1, 46), 1, 2]  # duplicates are sent once
    await harness.football.fixtures_by_ids(ids, purpose=AUTO)
    sent = [call.request.url.params["ids"] for call in route.calls]
    assert len(sent) == 3
    assert sent[0] == "-".join(str(i) for i in range(1, MAX_IDS_PER_REQUEST + 1))
    assert sent[2] == "41-42-43-44-45"
    assert await harness.football.fixtures_by_ids([], purpose=AUTO) == []


async def test_odds_pagination_fetches_every_page(
    harness: Harness, router: respx.MockRouter
) -> None:
    page_one = router.get("/odds", params={"page": "2"}).respond(
        json=load("odds_league_39_2024-10-26_page2.json")
    )
    first = router.get("/odds").respond(json=load("odds_league_39_2024-10-26_page1.json"))
    items = await harness.football.odds_by_league_date(
        39, 2024, date(2024, 10, 26), purpose=AUTO, cache_ttl=6 * 3600
    )
    assert [item.fixture.id for item in items] == [1208100, 1208105]
    assert first.call_count == 1
    assert page_one.call_count == 1
    assert "page" not in first.calls.last.request.url.params


async def test_pagination_has_a_safety_stop(
    harness: Harness, router: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.clients.api_football.MAX_PAGES", 2)
    body = load("odds_league_39_2024-10-26_page1.json")
    assert isinstance(body, dict)
    body["paging"] = {"current": 1, "total": 50}
    route = router.get("/odds").respond(json=body)
    await harness.football.odds_by_fixture(1208100, purpose=AUTO)
    assert route.call_count == 2


async def test_context_endpoints(harness: Harness, router: respx.MockRouter) -> None:
    router.get("/odds").respond(json=load("odds_fixture_1208100.json"))
    router.get("/injuries").respond(json=load("injuries_fixture_1208100.json"))
    router.get("/fixtures/lineups").respond(json=load("lineups_fixture_1208100.json"))
    router.get("/fixtures/statistics").respond(json=load("statistics_fixture_1208021.json"))

    odds = await harness.football.odds_by_fixture(1208100, purpose=AUTO)
    assert [bookmaker.name for bookmaker in odds[0].bookmakers][:3] == [
        "Pinnacle",
        "Bet365",
        "1xBet",
    ]
    injuries = await harness.football.injuries_by_fixture(1208100, purpose=AUTO)
    assert [injury.player.name for injury in injuries] == ["B. Saka", "D. Szoboszlai"]
    lineups = await harness.football.lineups(1208100, purpose=AUTO)
    assert len(lineups[0].start_xi) == 11
    stats = await harness.football.fixture_statistics(1208021, purpose=AUTO)
    assert stats[0].statistics[-1].value == "1.73"


async def test_injuries_by_league(harness: Harness, router: respx.MockRouter) -> None:
    route = router.get("/injuries").respond(json=load("injuries_fixture_1208100.json"))
    await harness.football.injuries_by_league(39, 2024, purpose=AUTO)
    assert route.calls.last.request.url.params["league"] == "39"


@pytest.mark.parametrize(
    ("name", "field"), [("error_token.json", "token"), ("error_plan.json", "plan")]
)
async def test_errors_in_body_raise(
    harness: Harness, router: respx.MockRouter, name: str, field: str
) -> None:
    router.get("/fixtures").respond(json=load(name))
    with pytest.raises(ApiFootballError) as error:
        await harness.football.fixtures_by_date(date(2024, 10, 26), purpose=AUTO)
    assert field in error.value.errors


async def test_error_responses_are_not_cached(harness: Harness, router: respx.MockRouter) -> None:
    route = router.get("/fixtures").mock(
        side_effect=[
            httpx.Response(200, json={"errors": {"plan": "Free plans do not have access"}}),
            httpx.Response(200, json=load("fixtures_empty.json")),
        ]
    )
    with pytest.raises(ApiFootballError, match="plan"):
        await harness.football.fixtures_by_date(date(2024, 10, 26), purpose=AUTO)
    assert await harness.football.fixtures_by_date(date(2024, 10, 26), purpose=AUTO) == []
    assert route.call_count == 2


async def test_error_list_form_is_reported(harness: Harness, router: respx.MockRouter) -> None:
    router.get("/fixtures").respond(json={"errors": ["bad parameter"], "response": []})
    with pytest.raises(ApiFootballError, match="bad parameter"):
        await harness.football.fixtures_by_date(date(2024, 10, 26), purpose=AUTO)


async def test_unexpected_envelope_is_an_external_error(
    harness: Harness, router: respx.MockRouter
) -> None:
    router.get("/fixtures").respond(json={"response": "nope"})
    with pytest.raises(ExternalServiceError, match="unexpected response format"):
        await harness.football.fixtures_by_date(date(2024, 10, 26), purpose=AUTO)


async def test_status_object_response_is_one_record(
    harness: Harness, router: respx.MockRouter
) -> None:
    router.get("/status").respond(json=load("status.json"))
    envelope = await harness.football.envelope("status", {}, purpose=AUTO)
    assert len(envelope.records) == 1
    assert harness.football.api.config.name == "api-football"


def test_envelope_requires_list_or_object_response() -> None:
    with pytest.raises(ValidationError):
        Envelope.model_validate({"response": 3})


async def test_production_wiring(router: respx.MockRouter) -> None:
    route = router.get("/fixtures").respond(json=load("fixtures_empty.json"))
    client, http = create_api_football_client(TEST_KEY, InMemoryUsageStore(), daily_limit=50)
    try:
        assert isinstance(http, httpx.AsyncClient)
        assert client.api.quota.policy.daily_limit == 50
        await client.fixtures_by_date(date(2024, 7, 1), purpose=AUTO)
        assert route.calls.last.request.headers["x-apisports-key"] == TEST_KEY
    finally:
        await http.aclose()
