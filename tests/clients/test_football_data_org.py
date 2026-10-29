"""Football-Data.org endpoint client over respx-mocked responses (no network)."""

import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from datetime import UTC, date, datetime
from itertools import pairwise
from pathlib import Path

import httpx
import pytest
import respx
from hypothesis import given
from hypothesis import strategies as st

from app.clients.football_data_org import (
    FOOTBALL_DATA_ORG_CONFIG,
    MAX_DAYS_PER_MATCHES_REQUEST,
    REQUESTS_PER_MINUTE,
    HttpFootballDataClient,
    competition_path,
    create_football_data_client,
    date_windows,
)
from app.clients.football_data_org_models import MatchStatus
from app.clients.http import ApiClient, ProviderHttpError, ProviderUnavailableError
from app.clients.quota import RequestPurpose
from app.errors import ConfigurationError, ExternalServiceError
from tests.clients.conftest import FakeTime, Harness, RecordingSleep, build_harness

AUTO = RequestPurpose.AUTOMATIC
BASE_URL = FOOTBALL_DATA_ORG_CONFIG.base_url
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "football_data_org"
TEST_KEY = "football-data-test-key-0123"

type Football = tuple[HttpFootballDataClient, Harness]


def load(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def football_router() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
async def football(
    fake_time: FakeTime,
    football_router: respx.MockRouter,  # noqa: ARG001 - requested so every request is mocked
) -> AsyncIterator[Football]:
    config = replace(FOOTBALL_DATA_ORG_CONFIG, requests_per_minute=600, burst=100)
    async with httpx.AsyncClient(base_url=BASE_URL, headers={config.auth_header: TEST_KEY}) as http:
        harness = build_harness(fake_time, http, config=config)
        yield HttpFootballDataClient(harness.api), harness


async def test_matches_between_parses_and_tolerates_odd_records(
    football: Football, football_router: respx.MockRouter
) -> None:
    client, _ = football
    route = football_router.get("matches").respond(json=load("matches_list.json"))
    matches = await client.matches_between(date(2024, 10, 26), date(2024, 10, 27), purpose=AUTO)
    assert [m.id for m in matches] == [1, 2]  # id 3 has no utcDate: skipped, not fatal
    assert matches[0].status is MatchStatus.TIMED
    assert matches[0].home_team.name == "Chelsea FC"
    assert matches[0].score.full_time.home is None
    assert matches[1].status is MatchStatus.UNKNOWN  # an unseen status does not break parsing
    assert matches[1].home_team.id is None  # cup team not drawn yet
    params = route.calls.last.request.url.params
    assert (params["dateFrom"], params["dateTo"]) == ("2024-10-26", "2024-10-27")


async def test_auth_header_is_sent(football: Football, football_router: respx.MockRouter) -> None:
    client, _ = football
    route = football_router.get("matches").respond(json={"matches": []})
    await client.matches_between(date(2024, 10, 26), date(2024, 10, 26), purpose=AUTO)
    assert route.calls.last.request.headers["X-Auth-Token"] == TEST_KEY


async def test_long_range_is_split_into_ten_day_windows(
    football: Football, football_router: respx.MockRouter
) -> None:
    client, _ = football
    route = football_router.get("matches").respond(json={"matches": []})
    await client.matches_between(date(2024, 10, 1), date(2024, 10, 25), purpose=AUTO)
    sent = [(c.request.url.params["dateFrom"], c.request.url.params["dateTo"]) for c in route.calls]
    assert sent == [
        ("2024-10-01", "2024-10-10"),
        ("2024-10-11", "2024-10-20"),
        ("2024-10-21", "2024-10-25"),
    ]


async def test_reversed_range_is_rejected_before_any_request(
    football: Football, football_router: respx.MockRouter
) -> None:
    client, _ = football
    route = football_router.get("matches").respond(json={"matches": []})
    with pytest.raises(ValueError, match="before"):
        await client.matches_between(date(2024, 10, 5), date(2024, 10, 1), purpose=AUTO)
    assert route.call_count == 0


async def test_competition_matches_defaults_to_scheduled(
    football: Football, football_router: respx.MockRouter
) -> None:
    client, _ = football
    route = football_router.get("competitions/PL/matches").respond(json=load("matches_list.json"))
    matches = await client.competition_matches("pl", purpose=AUTO)
    assert len(matches) == 2
    assert route.calls.last.request.url.params["status"] == "SCHEDULED"
    football_router.get("competitions/2021/matches").respond(json={"matches": []})
    await client.competition_matches(2021, purpose=AUTO, status=None)
    assert "status" not in football_router.calls.last.request.url.params


@pytest.mark.parametrize("bad", ["", "P/L", "../matches", "PL?x=1", "PL%2F", "TOOLONGCODE", "a b"])
async def test_competition_code_is_validated(football: Football, bad: str) -> None:
    client, _ = football
    with pytest.raises(ValueError, match="competition"):
        await client.competition_matches(bad, purpose=AUTO)


async def test_match_detail_and_lineups(
    football: Football, football_router: respx.MockRouter
) -> None:
    client, _ = football
    football_router.get("matches/497410").respond(json=load("match_detail_lineups.json"))
    match = await client.match(497410, purpose=AUTO)
    assert match.utc_date == datetime(2024, 10, 26, 11, 30, tzinfo=UTC)
    assert (match.score.full_time.home, match.score.full_time.away) == (1, 2)
    assert match.score.half_time.away == 1

    lineups = await client.lineups(497410, purpose=AUTO)  # second call: served from cache
    assert lineups.announced
    assert lineups.home.team_name == "Chelsea FC"
    assert lineups.home.formation == "4-2-3-1"
    assert lineups.home.coach is not None
    assert lineups.home.coach.name == "Enzo Maresca"
    palmer = lineups.home.starters[1]
    assert (palmer.name, palmer.position, palmer.shirt_number) == (
        "Cole Palmer",
        "Attacking Midfield",
        20,
    )
    assert [p.name for p in lineups.home.bench] == ["Filip Jorgensen"]
    assert lineups.away.bench == ()
    assert football_router.calls.call_count == 1


async def test_free_plan_without_lineups_is_not_announced(
    football: Football, football_router: respx.MockRouter
) -> None:
    client, _ = football
    body = {
        "id": 5,
        "utcDate": "2024-10-26T11:30:00Z",
        "status": "SCHEDULED",
        "homeTeam": {"id": 1, "name": "A", "coach": None, "lineup": [], "bench": []},
        "awayTeam": {"id": 2, "name": "B"},
    }
    football_router.get("matches/5").respond(json=body)
    lineups = await client.lineups(5, purpose=AUTO)
    assert not lineups.announced
    assert lineups.home.starters == ()
    assert lineups.away.coach is None


async def test_unknown_match_raises_http_error_with_status(
    football: Football, football_router: respx.MockRouter
) -> None:
    client, _ = football
    football_router.get("matches/999").respond(404, json={"message": "Resource not found"})
    with pytest.raises(ProviderHttpError) as info:
        await client.match(999, purpose=AUTO)
    assert info.value.status_code == 404


async def test_malformed_bodies_raise_and_are_not_cached(
    football: Football, football_router: respx.MockRouter
) -> None:
    client, _ = football
    detail = football_router.get("matches/7").respond(json={"unexpected": True})
    for _ in range(2):
        with pytest.raises(ExternalServiceError, match="unexpected response"):
            await client.match(7, purpose=AUTO)
    assert detail.call_count == 2  # the bad body was not replayed from cache

    listing = football_router.get("competitions/PL/matches").respond(json={"matches": "nope"})
    for _ in range(2):
        with pytest.raises(ExternalServiceError, match="no 'matches' list"):
            await client.competition_matches("PL", purpose=AUTO)
    assert listing.call_count == 2


async def test_429_is_retried_with_retry_after_then_succeeds(
    football: Football, football_router: respx.MockRouter
) -> None:
    client, harness = football
    football_router.get("competitions/PL/matches").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}, json={"message": "slow down"}),
            httpx.Response(200, json=load("matches_list.json")),
        ]
    )
    matches = await client.competition_matches("PL", purpose=AUTO)
    assert len(matches) == 2
    assert 7.0 in harness.sleep.delays


async def test_persistent_429_ends_in_unavailable_error(
    football: Football, football_router: respx.MockRouter
) -> None:
    client, _ = football
    football_router.get("matches/3").respond(429, json={"message": "limit"})
    with pytest.raises(ProviderUnavailableError):
        await client.match(3, purpose=AUTO)


async def test_factory_wires_header_rate_limit_and_no_daily_quota(
    football_router: respx.MockRouter,
) -> None:
    route = football_router.get("matches/1").respond(json=load("match_detail_lineups.json"))
    client, http = create_football_data_client(TEST_KEY)
    try:
        assert str(http.base_url) == BASE_URL
        assert FOOTBALL_DATA_ORG_CONFIG.requests_per_minute == REQUESTS_PER_MINUTE == 30
        await client.match(1, purpose=AUTO)
        assert route.calls.last.request.headers["X-Auth-Token"] == TEST_KEY
    finally:
        await http.aclose()


async def test_client_without_quota_never_blocks_and_has_no_quota_status(
    fake_time: FakeTime, football_router: respx.MockRouter
) -> None:
    config = replace(FOOTBALL_DATA_ORG_CONFIG, requests_per_minute=6000, burst=1000)
    route = football_router.get("matches").respond(json={"matches": []})
    async with httpx.AsyncClient(base_url=BASE_URL) as http:
        api = ApiClient(config, http=http, quota=None, sleep=RecordingSleep(fake_time))
        client = HttpFootballDataClient(api)
        # 200 uncached requests: far beyond any daily counter, none refused.
        for day in range(200):
            start = date.fromordinal(date(2024, 1, 1).toordinal() + day)
            await client.matches_between(start, start, purpose=AUTO)
        with pytest.raises(ConfigurationError, match="no daily quota"):
            _ = api.quota
    assert route.call_count == 200


@given(
    start=st.dates(min_value=date(2000, 1, 1), max_value=date(2100, 1, 1)),
    length=st.integers(min_value=0, max_value=400),
)
def test_date_windows_cover_the_range_exactly(start: date, length: int) -> None:
    end = date.fromordinal(start.toordinal() + length)
    windows = list(date_windows(start, end))
    assert windows[0][0] == start
    assert windows[-1][1] == end
    for (_, previous_end), (next_start, _) in pairwise(windows):
        assert next_start.toordinal() == previous_end.toordinal() + 1
    for window_start, window_end in windows:
        assert 0 <= (window_end - window_start).days < MAX_DAYS_PER_MATCHES_REQUEST


@given(st.text(max_size=12))
def test_competition_path_never_returns_unsafe_segment(text: str) -> None:
    try:
        segment = competition_path(text)
    except ValueError:
        return
    assert segment.isalnum()
    assert segment.isascii()
