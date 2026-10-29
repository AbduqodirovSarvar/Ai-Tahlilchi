"""Every Football-Data.org endpoint: the path asked, the query sent and the answer parsed.

Bodies follow the v4 reference examples (field names verbatim, values shortened).
"""

from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from datetime import date

import httpx
import pytest
import respx

from app.clients.football_data_org import FOOTBALL_DATA_ORG_CONFIG, HttpFootballDataClient
from app.clients.football_data_org_models import ErrorBody, Odds
from app.clients.football_data_org_requests import (
    CompetitionListFilters,
    CompetitionTeamsFilters,
    Head2HeadFilters,
    MatchFilters,
    PersonMatchFilters,
    ScorersFilters,
    StandingsFilters,
    StatusFilter,
    TeamMatchFilters,
)
from app.clients.http import ProviderHttpError
from app.clients.quota import RequestPurpose
from app.errors import ExternalServiceError
from tests.clients.conftest import FakeTime, build_harness

AUTO = RequestPurpose.AUTOMATIC
BASE_URL = FOOTBALL_DATA_ORG_CONFIG.base_url

TEAM_REF = {"id": 57, "name": "Arsenal FC", "shortName": "Arsenal", "tla": "ARS", "crest": "x"}
SEASON = {
    "id": 2287,
    "startDate": "2025-08-15",
    "endDate": "2026-05-24",
    "currentMatchday": 9,
    "winner": None,
    "stages": ["REGULAR_SEASON"],
}
COMPETITION = {"id": 2021, "name": "Premier League", "code": "PL", "type": "LEAGUE"}
MATCH = {
    "area": {"id": 2072, "name": "England", "code": "ENG", "flag": "f"},
    "competition": COMPETITION,
    "season": SEASON,
    "id": 537800,
    "utcDate": "2025-10-26T14:00:00Z",
    "status": "FINISHED",
    "minute": 90,
    "injuryTime": 4,
    "attendance": 60000,
    "venue": "Emirates Stadium",
    "matchday": 9,
    "stage": "REGULAR_SEASON",
    "group": None,
    "lastUpdated": "2025-10-26T16:00:00Z",
    "homeTeam": {**TEAM_REF, "leagueRank": 1, "formation": "4-3-3", "lineup": [], "bench": []},
    "awayTeam": {"id": 61, "name": "Chelsea FC"},
    "score": {
        "winner": "HOME_TEAM",
        "duration": "REGULAR",
        "fullTime": {"home": 2, "away": 1},
        "halfTime": {"home": 1, "away": 0},
    },
    "goals": [
        {
            "minute": 12,
            "injuryTime": None,
            "type": "REGULAR",
            "team": {"id": 57, "name": "Arsenal FC"},
            "scorer": {"id": 7, "name": "Bukayo Saka"},
            "assist": None,
            "score": {"home": 1, "away": 0},
        }
    ],
    "bookings": [{"minute": 40, "team": {"id": 61}, "player": {"id": 9}, "card": "YELLOW"}],
    "substitutions": [{"minute": 70, "playerOut": {"id": 7}, "playerIn": {"id": 8}}],
    "odds": {"homeWin": 1.85, "draw": 3.6, "awayWin": 4.2},
    "referees": [{"id": 11, "name": "M. Oliver", "type": "REFEREE", "nationality": "England"}],
}


@pytest.fixture
def api_router() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
async def client(
    fake_time: FakeTime,
    api_router: respx.MockRouter,  # noqa: ARG001 - requested so every request is mocked
) -> AsyncIterator[HttpFootballDataClient]:
    config = replace(FOOTBALL_DATA_ORG_CONFIG, requests_per_minute=600, burst=100)
    async with httpx.AsyncClient(base_url=BASE_URL) as http:
        yield HttpFootballDataClient(build_harness(fake_time, http, config=config).api)


async def test_areas_and_area(client: HttpFootballDataClient, api_router: respx.MockRouter) -> None:
    api_router.get("areas").respond(json={"count": 1, "areas": [{"id": 2072, "name": "England"}]})
    api_router.get("areas/2267").respond(
        json={
            "id": 2267,
            "name": "World",
            "parentAreaId": None,
            "childAreas": [{"id": 2001, "name": "Africa"}],
        }
    )
    assert (await client.areas(purpose=AUTO)).areas[0].name == "England"
    world = await client.area(2267, purpose=AUTO)
    assert [child.name for child in world.child_areas] == ["Africa"]


async def test_competitions_and_competition(
    client: HttpFootballDataClient, api_router: respx.MockRouter
) -> None:
    listing = api_router.get("competitions").respond(
        json={
            "count": 1,
            "competitions": [
                {
                    **COMPETITION,
                    "plan": "TIER_ONE",
                    "currentSeason": SEASON,
                    "numberOfAvailableSeasons": 30,
                }
            ],
        }
    )
    api_router.get("competitions/PL").respond(json={**COMPETITION, "seasons": [SEASON]})
    found = await client.competitions(CompetitionListFilters(areas=frozenset({2072})), purpose=AUTO)
    assert listing.calls.last.request.url.params["areas"] == "2072"
    entry = found.competitions[0]
    assert entry.plan == "TIER_ONE"
    assert entry.current_season is not None
    assert entry.current_season.starting_year == 2025
    detail = await client.competition("pl", purpose=AUTO)
    assert detail.seasons[0].current_matchday == 9


async def test_standings(client: HttpFootballDataClient, api_router: respx.MockRouter) -> None:
    route = api_router.get("competitions/PL/standings").respond(
        json={
            "competition": COMPETITION,
            "season": SEASON,
            "standings": [
                {
                    "stage": "REGULAR_SEASON",
                    "type": "TOTAL",
                    "group": None,
                    "table": [
                        {
                            "position": 1,
                            "team": TEAM_REF,
                            "playedGames": 9,
                            "form": "W,W,D,W,W",
                            "won": 7,
                            "draw": 1,
                            "lost": 1,
                            "points": 22,
                            "goalsFor": 18,
                            "goalsAgainst": 5,
                            "goalDifference": 13,
                        }
                    ],
                },
                {"stage": "REGULAR_SEASON", "type": "HOME", "table": []},
            ],
        }
    )
    standings = await client.standings("PL", StandingsFilters(matchday=9), purpose=AUTO)
    assert route.calls.last.request.url.params["matchday"] == "9"
    [leader] = standings.table()
    assert (leader.position, leader.points, leader.goal_difference) == (1, 22, 13)
    assert standings.table("HOME") == ()
    assert standings.table("AWAY") == ()


async def test_scorers_and_teams(
    client: HttpFootballDataClient, api_router: respx.MockRouter
) -> None:
    api_router.get("competitions/PL/scorers").respond(
        json={
            "count": 1,
            "competition": COMPETITION,
            "season": SEASON,
            "scorers": [
                {
                    "player": {"id": 7, "name": "Bukayo Saka", "position": "Offence"},
                    "team": TEAM_REF,
                    "playedMatches": 9,
                    "goals": 6,
                    "assists": 4,
                    "penalties": None,
                }
            ],
        }
    )
    teams = api_router.get("competitions/PL/teams").respond(
        json={
            "count": 1,
            "competition": COMPETITION,
            "season": SEASON,
            "teams": [
                {
                    **TEAM_REF,
                    "founded": 1886,
                    "clubColors": "Red / White",
                    "coach": {"id": 1, "name": "Mikel Arteta", "contract": {"until": "2027-06"}},
                    "squad": [{"id": 7, "name": "Bukayo Saka", "shirtNumber": 7}],
                }
            ],
        }
    )
    scorers = await client.scorers("PL", ScorersFilters(limit=5), purpose=AUTO)
    assert scorers.scorers[0].goals == 6
    found = await client.competition_teams("PL", CompetitionTeamsFilters(season=2025), purpose=AUTO)
    assert teams.calls.last.request.url.params["season"] == "2025"
    team = found.teams[0]
    assert team.coach is not None
    assert team.coach.contract is not None
    assert team.coach.contract.until == "2027-06"
    assert team.squad[0].shirt_number == 7


async def test_team_person_and_their_matches(
    client: HttpFootballDataClient, api_router: respx.MockRouter
) -> None:
    api_router.get("teams/57").respond(json={**TEAM_REF, "venue": "Emirates Stadium"})
    team_matches = api_router.get("teams/57/matches").respond(json={"matches": [MATCH]})
    api_router.get("persons/7").respond(
        json={"id": 7, "name": "Bukayo Saka", "dateOfBirth": "2001-09-05", "currentTeam": TEAM_REF}
    )
    person_matches = api_router.get("persons/7/matches").respond(json={"matches": []})

    assert (await client.team(57, purpose=AUTO)).venue == "Emirates Stadium"
    await client.team_matches(57, TeamMatchFilters(status=StatusFilter.SCHEDULED), purpose=AUTO)
    assert team_matches.calls.last.request.url.params["status"] == "SCHEDULED"
    person = await client.person(7, purpose=AUTO)
    assert person.date_of_birth == date(2001, 9, 5)
    assert person.current_team is not None
    await client.person_matches(7, PersonMatchFilters(limit=3), purpose=AUTO)
    assert person_matches.calls.last.request.url.params["limit"] == "3"


async def test_full_match_details_are_parsed(
    client: HttpFootballDataClient, api_router: respx.MockRouter
) -> None:
    api_router.get("matches/537800").respond(json=MATCH)
    match = await client.match(537800, purpose=AUTO)
    assert match.score.winner == "HOME_TEAM"
    assert match.goals[0].scorer is not None
    assert match.goals[0].scorer.name == "Bukayo Saka"
    assert match.bookings[0].card == "YELLOW"
    assert match.substitutions[0].player_in is not None
    assert match.referees[0].type == "REFEREE"
    assert match.home_team.league_rank == 1
    assert match.odds.available
    assert (match.odds.home_win, match.odds.draw, match.odds.away_win) == (1.85, 3.6, 4.2)


def test_odds_without_the_package_are_absent() -> None:
    odds = Odds.model_validate({"msg": "Activate Odds-Package in User-Panel to retrieve odds."})
    assert not odds.available


async def test_matches_by_ids_and_by_day(
    client: HttpFootballDataClient, api_router: respx.MockRouter
) -> None:
    route = api_router.get("matches").respond(json={"matches": [MATCH]})
    found = await client.matches(MatchFilters(ids=(537800,)), purpose=AUTO)
    assert [m.id for m in found] == [537800]
    assert route.calls.last.request.url.params["ids"] == "537800"
    await client.matches(MatchFilters(day=date(2025, 10, 26)), purpose=AUTO)
    assert route.calls.last.request.url.params["date"] == "2025-10-26"


async def test_head2head(client: HttpFootballDataClient, api_router: respx.MockRouter) -> None:
    route = api_router.get("matches/537800/head2head").respond(
        json={
            "aggregates": {
                "numberOfMatches": 2,
                "totalGoals": 5,
                "homeTeam": {"id": 57, "name": "Arsenal FC", "wins": 1, "draws": 1, "losses": 0},
                "awayTeam": {"id": 61, "name": "Chelsea FC", "wins": 0, "draws": 1, "losses": 1},
            },
            "matches": [MATCH, {"id": "broken"}],
        }
    )
    h2h = await client.head2head(537800, Head2HeadFilters(limit=10), purpose=AUTO)
    assert route.calls.last.request.url.params["limit"] == "10"
    assert h2h.aggregates.number_of_matches == 2
    assert h2h.aggregates.home_team is not None
    assert h2h.aggregates.home_team.wins == 1
    assert [m.id for m in h2h.matches] == [537800]  # the broken record is skipped


@pytest.mark.parametrize("status", [400, 403, 404])
async def test_client_errors_keep_their_status(
    client: HttpFootballDataClient, api_router: respx.MockRouter, status: int
) -> None:
    api_router.get("teams/1").respond(status, json={"message": "nope", "errorCode": status})
    with pytest.raises(ProviderHttpError) as info:
        await client.team(1, purpose=AUTO)
    assert info.value.status_code == status


def test_error_body_reads_both_documented_shapes() -> None:
    assert ErrorBody.model_validate({"error": "Argument 'id' is wrong"}).text == (
        "Argument 'id' is wrong"
    )
    live = ErrorBody.model_validate({"message": "Restricted", "errorCode": 403})
    assert (live.text, live.error_code) == ("Restricted", 403)


async def test_malformed_object_is_not_cached(
    client: HttpFootballDataClient, api_router: respx.MockRouter
) -> None:
    route = api_router.get("competitions/PL/standings").respond(json={"standings": "nope"})
    for _ in range(2):
        with pytest.raises(ExternalServiceError):
            await client.standings("PL", StandingsFilters(), purpose=AUTO)
    assert route.call_count == 2


@pytest.mark.parametrize("bad", [0, -1, True])
async def test_bad_ids_never_reach_a_url(client: HttpFootballDataClient, bad: int) -> None:
    with pytest.raises(ValueError, match="resource id"):
        await client.team(bad, purpose=AUTO)
