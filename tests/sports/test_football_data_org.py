"""FootballDataAdapter: Football-Data.org matches mapped to domain fixtures, results and odds."""

from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.clients.football_data_org_models import Match, MatchLineups, MatchStatus, match_lineups
from app.clients.football_data_org_requests import (
    CompetitionMatchFilters,
    MatchFilters,
    TeamMatchFilters,
)
from app.clients.http import ProviderHttpError
from app.clients.quota import RequestPurpose
from app.domain import (
    BOOKMAKER_MARKET_AVERAGE,
    ExternalRef,
    FixtureStatus,
    MarketType,
    OddsKind,
    ResultStatus,
    Selection,
)
from app.sports.base import FixtureQuery
from app.sports.football_data_org import (
    MAX_IDS_PER_REQUEST,
    SOURCE,
    FootballDataAdapter,
    card_counts,
    fixture_status,
    main_referee,
    map_result,
)

NOW = datetime(2026, 10, 29, 8, 0, tzinfo=UTC)
DAY = date(2026, 10, 30)


def match(match_id: int = 1, *, code: str | None = "PL", **extra: Any) -> Match:
    body: dict[str, Any] = {
        "id": match_id,
        "utcDate": "2026-10-30T16:00:00Z",
        "status": "TIMED",
        "area": {"id": 2072, "name": "England"},
        "competition": None
        if code is None
        else {"id": 2021, "name": "Premier League", "code": code},
        "season": {"startDate": "2026-08-14"},
        "homeTeam": {"id": 57, "name": "Arsenal FC"},
        "awayTeam": {"id": 61, "name": "Chelsea FC"},
    }
    body.update(extra)
    return Match.model_validate(body)


class StubClient:
    """The client methods the adapter uses, answering from memory."""

    def __init__(self, matches: list[Match] | None = None) -> None:
        self.matches_ = matches or []
        self.calls: list[tuple[str, object]] = []
        self.season_error: Exception | None = None
        self.fresh: list[bool] = []

    async def matches(
        self, filters: MatchFilters, *, fresh: bool = False, **_: object
    ) -> list[Match]:
        self.calls.append(("matches", filters))
        self.fresh.append(fresh)
        if filters.ids:
            return [m for m in self.matches_ if m.id in filters.ids]
        return list(self.matches_)

    async def team_matches(
        self, team_id: int, _filters: TeamMatchFilters, **_: RequestPurpose
    ) -> list[Match]:
        self.calls.append(("team", team_id))
        return [m for m in self.matches_ if team_id in (m.home_team.id, m.away_team.id)]

    async def competition_matches(
        self, competition: str | int, filters: CompetitionMatchFilters, **_: RequestPurpose
    ) -> list[Match]:
        self.calls.append(("season", (competition, filters.season)))
        if self.season_error is not None and competition == "BJL":
            raise self.season_error
        return [m for m in self.matches_ if m.competition and m.competition.code == competition]

    async def lineups(self, match_id: int, **_: RequestPurpose) -> MatchLineups:
        return match_lineups(next(m for m in self.matches_ if m.id == match_id))


def adapter(client: StubClient) -> FootballDataAdapter:
    return FootballDataAdapter(client, clock=lambda: NOW)  # type: ignore[arg-type]


async def test_fixtures_of_tracked_leagues_only_and_undecided_teams_skipped() -> None:
    client = StubClient(
        [
            match(1),
            match(2, code="CL"),  # Champions League: not a tracked league
            match(3, homeTeam={"id": None, "name": None}),  # cup tie not drawn yet
            match(4, utcDate="2026-11-05T16:00:00Z"),  # outside the asked range
        ]
    )
    fixtures = await adapter(client).fetch_fixtures(FixtureQuery(date_from=DAY, date_to=DAY))
    assert [f.ref for f in fixtures] == [ExternalRef(SOURCE, "1")]
    fixture = fixtures[0]
    assert fixture.league.ref == ExternalRef(SOURCE, "PL")
    assert fixture.league.country == "England"
    assert fixture.home.ref == ExternalRef(SOURCE, "57")
    assert fixture.season == 2026
    assert fixture.status is FixtureStatus.SCHEDULED
    assert client.calls == [("matches", MatchFilters(date_from=DAY, date_to=DAY))]


async def test_league_scope_and_untracked_league_refs() -> None:
    client = StubClient([match(1), match(2, code="PD")])
    query = FixtureQuery(
        date_from=DAY,
        date_to=DAY,
        leagues=frozenset({ExternalRef("football-data", "SP1"), ExternalRef("x", "nope")}),
    )
    fixtures = await adapter(client).fetch_fixtures(query)
    assert [f.ref.external_id for f in fixtures] == ["2"]  # history ref E0/SP1 resolves too


async def test_team_scope_uses_provider_team_ids_only() -> None:
    other = {"homeTeam": {"id": 98, "name": "X"}, "awayTeam": {"id": 99, "name": "Y"}}
    client = StubClient([match(1), match(5, **other)])
    query = FixtureQuery(
        date_from=DAY,
        date_to=DAY,
        teams=frozenset({ExternalRef(SOURCE, "61"), ExternalRef("football-data", "Chelsea")}),
    )
    fixtures = await adapter(client).fetch_fixtures(query)
    assert [f.ref.external_id for f in fixtures] == ["1"]
    assert ("team", 61) in client.calls
    assert not any(name == "matches" for name, _ in client.calls)  # no league sweep


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("SCHEDULED", FixtureStatus.SCHEDULED),
        ("IN_PLAY", FixtureStatus.LIVE),
        ("PENALTY_SHOOTOUT", FixtureStatus.LIVE),
        ("FINISHED", FixtureStatus.FINISHED),
        ("AWARDED", FixtureStatus.FINISHED),
        ("POSTPONED", FixtureStatus.POSTPONED),
        ("SUSPENDED", FixtureStatus.ABANDONED),
        ("CANCELLED", FixtureStatus.CANCELLED),
        ("BRAND_NEW", FixtureStatus.SCHEDULED),
    ],
)
def test_status_mapping(status: str, expected: FixtureStatus) -> None:
    assert fixture_status(MatchStatus(status)) is expected


def test_result_is_the_90_minute_score() -> None:
    regular = map_result(
        match(
            status="FINISHED",
            score={
                "duration": "REGULAR",
                "fullTime": {"home": 2, "away": 1},
                "halfTime": {"home": 1, "away": 1},
            },
        )
    )
    assert regular is not None
    assert (regular.home_score, regular.away_score, regular.home_ht_score) == (2, 1, 1)

    extra = map_result(
        match(
            status="FINISHED",
            score={
                "duration": "EXTRA_TIME",
                "fullTime": {"home": 2, "away": 1},
                "regularTime": {"home": 1, "away": 1},
                "halfTime": {"home": 0, "away": 0},
            },
        )
    )
    assert extra is not None
    assert (extra.home_score, extra.away_score) == (1, 1)

    unknown = match(
        status="FINISHED",
        score={"duration": "PENALTY_SHOOTOUT", "fullTime": {"home": 5, "away": 4}},
    )
    assert map_result(unknown) is None  # never guess the 90-minute score


def test_postponed_and_live_results() -> None:
    postponed = map_result(match(status="POSTPONED"))
    assert postponed is not None
    assert postponed.status is ResultStatus.POSTPONED
    assert postponed.home_score is None
    assert map_result(match(status="IN_PLAY")) is None


async def test_results_and_odds_by_id_in_batches() -> None:
    finished = [
        match(i, status="FINISHED", score={"fullTime": {"home": 1, "away": 0}})
        for i in range(1, 46)
    ]
    client = StubClient(finished)
    refs = [ExternalRef(SOURCE, str(i)) for i in range(1, 46)] + [ExternalRef("other", "1")]
    results = await adapter(client).fetch_results(refs)
    assert len(results) == 45
    batches = [f for name, f in client.calls if name == "matches"]
    assert [len(f.ids) for f in batches] == [MAX_IDS_PER_REQUEST, MAX_IDS_PER_REQUEST, 5]
    assert client.fresh == [True, True, True]  # a result is never answered from the cache


async def test_odds_only_with_the_package() -> None:
    priced = match(1, odds={"homeWin": 1.9, "draw": 3.5, "awayWin": 4.1})
    unpriced = match(2, odds={"msg": "Activate Odds-Package in User-Panel to retrieve odds."})
    broken = match(3, odds={"homeWin": 1.0, "draw": 3.5, "awayWin": 4.1})
    client = StubClient([priced, unpriced, broken])
    snapshots = await adapter(client).fetch_odds(
        [ExternalRef(SOURCE, str(i)) for i in (1, 2, 3)], kind=OddsKind.MORNING
    )
    assert {(s.selection, s.odds) for s in snapshots} == {
        (Selection.HOME, 1.9),
        (Selection.DRAW, 3.5),
        (Selection.AWAY, 4.1),
    }
    assert {s.bookmaker for s in snapshots} == {BOOKMAKER_MARKET_AVERAGE}
    assert {s.market.type for s in snapshots} == {MarketType.MATCH_RESULT}
    assert all(s.taken_at == NOW and s.kind is OddsKind.MORNING for s in snapshots)
    assert client.fresh == [False]  # a morning price may come from the cache
    await adapter(client).fetch_odds([ExternalRef(SOURCE, "1")], kind=OddsKind.CLOSING)
    assert client.fresh[-1] is True  # a closing price never does


async def test_history_skips_leagues_outside_the_plan() -> None:
    client = StubClient([match(1, status="FINISHED", score={"fullTime": {"home": 0, "away": 0}})])
    client.season_error = ProviderHttpError("football-data-org", 403, "restricted")
    batch = await adapter(client).refresh_history(
        [
            ExternalRef("football-data", "E0"),
            ExternalRef("football-data", "B1"),
            ExternalRef("x", "y"),
        ],
        2026,
    )
    assert [f.ref.external_id for f in batch.fixtures] == ["1"]
    assert len(batch.results) == 1
    assert ("season", ("PL", 2026)) in client.calls


async def test_history_reraises_other_errors() -> None:
    client = StubClient([])
    client.season_error = ProviderHttpError("football-data-org", 500, "boom")
    with pytest.raises(ProviderHttpError):
        await adapter(client).refresh_history([ExternalRef("football-data", "B1")], 2026)


async def test_lineups_confirmed() -> None:
    full = match(
        1,
        homeTeam={"id": 57, "name": "A", "lineup": [{"name": "x"}]},
        awayTeam={"id": 61, "name": "B", "lineup": [{"name": "y"}]},
    )
    client = StubClient([full, match(2)])
    assert await adapter(client).lineups_confirmed(ExternalRef(SOURCE, "1"))
    assert not await adapter(client).lineups_confirmed(ExternalRef(SOURCE, "2"))
    assert not await adapter(client).lineups_confirmed(ExternalRef("other", "1"))


def test_markets_and_purpose() -> None:
    base = adapter(StubClient())
    assert base.supported_markets() == frozenset({MarketType.MATCH_RESULT})
    assert base.for_purpose(RequestPurpose.MANUAL).sport is base.sport


def test_bookings_become_cards_and_the_referee_is_kept() -> None:
    booked = match(
        status="FINISHED",
        score={"fullTime": {"home": 1, "away": 1}},
        bookings=[
            {"team": {"id": 57}, "card": "YELLOW"},
            {"team": {"id": 57}, "card": "YELLOW"},
            {"team": {"id": 61}, "card": "YELLOW"},
            {"team": {"id": 61}, "card": "YELLOW_RED"},  # a second yellow counts as a red
            {"team": {"id": 999}, "card": "YELLOW"},  # not a team of this match
            {"team": None, "card": "RED"},
            {"team": {"id": 57}, "card": "PINK"},  # an unknown card
        ],
        referees=[
            {"name": "VAR Person", "type": "VIDEO_ASSISTANT_REFEREE_N1"},
            {"name": "Michael Oliver", "type": "REFEREE"},
        ],
    )
    assert card_counts(booked) == (2, 1, 0, 1)
    assert main_referee(booked) == "Michael Oliver"
    result = map_result(booked)
    assert result is not None
    assert (result.home_yellow, result.away_yellow, result.home_red, result.away_red) == (
        2,
        1,
        0,
        1,
    )
    assert result.referee == "Michael Oliver"


def test_no_bookings_means_unknown_not_zero() -> None:
    quiet = match(status="FINISHED", score={"fullTime": {"home": 0, "away": 0}})
    assert card_counts(quiet) is None
    assert main_referee(quiet) is None
    result = map_result(quiet)
    assert result is not None
    assert result.home_yellow is None
