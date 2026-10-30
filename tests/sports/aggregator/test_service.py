"""Data aggregator service: matches, odds and line-ups combined, failures contained."""

from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from typing import Any

import pytest

from app.clients.football_data_org_models import Match, MatchLineups, match_lineups
from app.clients.quota import RequestPurpose
from app.errors import DataError, ExternalServiceError
from app.sports.aggregator.csv_odds import ParsedOdds
from app.sports.aggregator.models import LinkStatus, MatchOdds, OddsRow
from app.sports.aggregator.service import (
    LINEUP_LOOKAHEAD,
    DataAggregatorService,
    check_range,
    lineups_due,
)

NOW = datetime(2026, 10, 31, 14, 0, tzinfo=UTC)
DAY = date(2026, 10, 31)
ODDS = MatchOdds(home_win=1.8, draw=3.75, away_win=4.5)
ELEVEN = [
    {"id": n, "name": f"Player {n}", "position": "Defence", "shirtNumber": n} for n in range(1, 12)
]


def match(match_id: int, home: tuple[int, str], away: tuple[int, str], **extra: Any) -> Match:
    body: dict[str, Any] = {
        "id": match_id,
        "utcDate": "2026-10-31T15:00:00Z",
        "status": "TIMED",
        "competition": {"id": 2021, "name": "Premier League", "code": "PL"},
        "homeTeam": {"id": home[0], "name": home[1]},
        "awayTeam": {"id": away[0], "name": away[1]},
    }
    body.update(extra)
    return Match.model_validate(body)


ARSENAL = match(1, (57, "Arsenal FC"), (61, "Chelsea FC"))
LATER = match(
    2, (66, "Manchester United FC"), (73, "Tottenham Hotspur FC"), utcDate="2026-10-31T20:00:00Z"
)
CUP = match(
    3,
    (5, "FC Bayern München"),
    (65, "Manchester City FC"),
    competition={"id": 2001, "name": "UEFA Champions League", "code": "CL"},
)


class FakeClient:
    def __init__(self, matches: list[Match], *, lineups_error: bool = False) -> None:
        self.matches = matches
        self.lineup_calls: list[int] = []
        self.lineups_error = lineups_error

    async def matches_between(self, _from: date, _to: date, **_: RequestPurpose) -> list[Match]:
        return self.matches

    async def lineups(self, match_id: int, **_: RequestPurpose) -> MatchLineups:
        self.lineup_calls.append(match_id)
        if self.lineups_error:
            raise ExternalServiceError("football-data-org: HTTP 503")
        found = next(m for m in self.matches if m.id == match_id)
        body = found.model_dump(by_alias=True, mode="json")
        body["homeTeam"]["lineup"] = ELEVEN
        return match_lineups(Match.model_validate(body))


class FakeOdds:
    def __init__(
        self,
        upcoming: list[OddsRow],
        *,
        season_error: Exception | None = None,
        upcoming_error: Exception | None = None,
    ) -> None:
        self._upcoming = upcoming
        self._season_error = season_error
        self._upcoming_error = upcoming_error
        self.season_calls: list[tuple[str, int]] = []
        self.upcoming_calls = 0

    async def season(self, division: str, season: int) -> ParsedOdds:
        self.season_calls.append((division, season))
        if self._season_error:
            raise self._season_error
        teams = ["Arsenal", "Chelsea", "Man United", "Tottenham", "Man City"]
        rows = [OddsRow("E0", date(2026, 8, 22), home, away) for home, away in pairwise(teams)]
        return ParsedOdds(tuple(rows))

    async def upcoming(self) -> ParsedOdds:
        self.upcoming_calls += 1
        if self._upcoming_error:
            raise self._upcoming_error
        return ParsedOdds(tuple(self._upcoming))


def service(client: FakeClient, odds: FakeOdds, **kwargs: Any) -> DataAggregatorService:
    return DataAggregatorService(client, odds, clock=lambda: NOW, **kwargs)  # type: ignore[arg-type]


async def test_matches_come_sorted_with_odds_and_link_status() -> None:
    client = FakeClient([LATER, CUP, ARSENAL])
    odds = FakeOdds([OddsRow("E0", DAY, "Arsenal", "Chelsea", odds=ODDS)])
    combined = await service(client, odds).combined_matches(DAY, DAY)

    assert [m.match_id for m in combined] == [1, 3, 2]
    arsenal, cup, later = combined
    assert arsenal.odds == ODDS
    assert arsenal.link is LinkStatus.MATCHED
    assert (arsenal.league, arsenal.league_code) == ("Premier League", "PL")
    assert (arsenal.home_team, arsenal.away_team) == ("Arsenal FC", "Chelsea FC")
    assert arsenal.match_date == datetime(2026, 10, 31, 15, tzinfo=UTC)
    assert cup.link is LinkStatus.LEAGUE_NOT_COVERED
    assert cup.odds is None
    assert later.link is LinkStatus.NO_ROW
    assert odds.upcoming_calls == 1
    assert odds.season_calls == [("E0", 2026)]


async def test_lineups_are_fetched_only_near_kick_off() -> None:
    client = FakeClient([ARSENAL, LATER])
    combined = await service(client, FakeOdds([])).combined_matches(DAY, DAY)
    assert client.lineup_calls == [1]  # 20:00 is beyond the look-ahead from 14:00
    assert combined[0].home_lineup is not None
    assert len(combined[0].home_lineup.starters) == 11
    assert combined[0].away_lineup is None  # not announced yet
    assert combined[1].home_lineup is None


async def test_lineup_budget_and_failures_never_drop_matches() -> None:
    near = [match(10 + n, (57, "Arsenal FC"), (61, "Chelsea FC")) for n in range(3)]
    client = FakeClient(near, lineups_error=True)
    combined = await service(client, FakeOdds([]), max_lineup_requests=2).combined_matches(DAY, DAY)
    assert client.lineup_calls == [10, 11]
    assert len(combined) == 3
    assert all(m.home_lineup is None for m in combined)


async def test_listed_lineups_cost_no_request() -> None:
    listed = match(
        20,
        (57, "Arsenal FC"),
        (61, "Chelsea FC"),
        homeTeam={"id": 57, "name": "Arsenal FC", "lineup": ELEVEN},
    )
    client = FakeClient([listed])
    [combined] = await service(client, FakeOdds([])).combined_matches(DAY, DAY)
    assert client.lineup_calls == []
    assert combined.home_lineup is not None


@pytest.mark.parametrize("error", [ExternalServiceError("down"), DataError("not a csv")])
async def test_csv_failures_leave_matches_without_odds(error: Exception) -> None:
    odds = FakeOdds([], season_error=error, upcoming_error=error)
    [combined] = await service(FakeClient([ARSENAL]), odds).combined_matches(DAY, DAY)
    assert combined.link is LinkStatus.SOURCE_UNAVAILABLE
    assert combined.odds is None


async def test_one_failed_file_still_uses_the_other() -> None:
    odds = FakeOdds(
        [OddsRow("E0", DAY, "Arsenal", "Chelsea", odds=ODDS)],
        season_error=ExternalServiceError("x"),
    )
    [combined] = await service(FakeClient([ARSENAL]), odds).combined_matches(DAY, DAY)
    assert combined.link is LinkStatus.MATCHED


async def test_no_tracked_league_means_no_csv_download() -> None:
    odds = FakeOdds([])
    [combined] = await service(FakeClient([CUP]), odds).combined_matches(DAY, DAY)
    assert combined.link is LinkStatus.LEAGUE_NOT_COVERED
    assert odds.upcoming_calls == 0


async def test_provider_failure_is_raised() -> None:
    class Down(FakeClient):
        async def matches_between(self, _from: date, _to: date, **_: RequestPurpose) -> list[Match]:
            raise ExternalServiceError("football-data-org: HTTP 429")

    with pytest.raises(ExternalServiceError):
        await service(Down([]), FakeOdds([])).combined_matches(DAY, DAY)


@pytest.mark.parametrize(
    ("day_from", "day_to"), [(DAY, DAY - timedelta(days=1)), (DAY, DAY + timedelta(days=10))]
)
def test_bad_ranges_are_refused(day_from: date, day_to: date) -> None:
    with pytest.raises(ValueError):
        check_range(day_from, day_to)


def test_lineups_due_window() -> None:
    assert lineups_due(ARSENAL, NOW)
    assert not lineups_due(ARSENAL, NOW - LINEUP_LOOKAHEAD - timedelta(hours=2))
    assert not lineups_due(match(30, (57, "A"), (61, "B"), status="FINISHED"), NOW)
    assert not lineups_due(ARSENAL, NOW + timedelta(hours=5))
