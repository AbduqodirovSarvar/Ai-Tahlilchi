"""League catalog, seasons and cross-source team matching."""

from datetime import UTC, date, datetime

import pytest

from app.domain import ExternalRef, Fixture, League, Sport, Team
from app.sports.football_leagues import (
    DEFAULT_LEAGUES,
    LeagueCatalog,
    TrackedLeague,
    season_for,
    seasons_between,
)
from app.sports.team_matching import (
    TEAM_ALIASES,
    IdentityMap,
    MatchMethod,
    match_teams,
)


def api_team(team_id: int, name: str) -> Team:
    return Team(ref=ExternalRef("api-football", str(team_id)), sport=Sport.FOOTBALL, name=name)


def fd_team(name: str) -> Team:
    return Team(ref=ExternalRef("football-data", name), sport=Sport.FOOTBALL, name=name)


HISTORY = [
    fd_team(name)
    for name in (
        "Man United",
        "Man City",
        "Nott'm Forest",
        "Arsenal",
        "Tottenham",
        "Wolves",
        "Newcastle",
        "Leicester",
        "Brighton",
        "West Ham",
        "Sheffield United",
    )
]


def test_default_leagues_are_the_ten_agreed_ones() -> None:
    catalog = LeagueCatalog()
    assert [league.api_id for league in catalog.leagues] == [
        39,
        40,
        140,
        78,
        135,
        61,
        88,
        94,
        144,
        203,
    ]
    epl = catalog.resolve(ExternalRef("football-data", "E0"))
    assert epl is not None
    assert epl.api_ref == ExternalRef("api-football", "39")
    assert catalog.by_api_id(203) == TrackedLeague(203, "T1")
    assert catalog.resolve(ExternalRef("api-football", "999")) is None
    assert catalog.by_api_id(999) is None
    assert epl.football_data_league().name == "Premier League"


def test_catalog_rejects_duplicates() -> None:
    with pytest.raises(ValueError, match="unique"):
        LeagueCatalog([*DEFAULT_LEAGUES, TrackedLeague(39, "X1")])


@pytest.mark.parametrize(
    ("day", "season"),
    [(date(2024, 7, 1), 2024), (date(2024, 6, 30), 2023), (date(2025, 3, 1), 2024)],
)
def test_season_for(day: date, season: int) -> None:
    assert season_for(day) == season


def test_window_across_july_spans_two_seasons() -> None:
    assert seasons_between(date(2025, 6, 25), date(2025, 7, 5)) == (2024, 2025)
    assert seasons_between(date(2025, 8, 1), date(2025, 8, 5)) == (2025,)


def test_aliases_exact_and_fuzzy_matches() -> None:
    report = match_teams(
        [
            api_team(33, "Manchester United"),
            api_team(42, "Arsenal"),
            api_team(47, "Tottenham Hotspur"),
            api_team(65, "Nottingham Forest"),
            api_team(62, "Sheffield Utd"),
        ],
        HISTORY,
    )
    methods = {m.provider.name: (m.history.name, m.method) for m in report.matched}
    assert methods["Manchester United"] == ("Man United", MatchMethod.ALIAS)
    assert methods["Arsenal"] == ("Arsenal", MatchMethod.EXACT)
    assert methods["Tottenham Hotspur"] == ("Tottenham", MatchMethod.FUZZY)
    assert methods["Nottingham Forest"] == ("Nott'm Forest", MatchMethod.ALIAS)
    assert methods["Sheffield Utd"] == ("Sheffield United", MatchMethod.ALIAS)
    assert report.unmatched == ()


def test_unknown_team_is_reported_unmatched() -> None:
    report = match_teams([api_team(1, "Real Madrid")], HISTORY)
    assert report.matched == ()
    assert [team.name for team in report.unmatched] == ["Real Madrid"]


def test_close_candidates_are_ambiguous_not_merged() -> None:
    report = match_teams(
        [api_team(1, "Manchester")], [fd_team("Manchester A"), fd_team("Manchester B")]
    )
    assert report.matched == ()
    assert len(report.ambiguous) == 1
    assert {team.name for team in report.ambiguous[0].candidates} == {
        "Manchester A",
        "Manchester B",
    }


def test_two_provider_teams_claiming_one_history_team_are_ambiguous() -> None:
    report = match_teams(
        [api_team(1, "Wolves"), api_team(2, "Wolverhampton Wolves")], [fd_team("Wolves")]
    )
    assert report.matched == ()
    assert {a.provider.name for a in report.ambiguous} == {"Wolves", "Wolverhampton Wolves"}


def test_duplicate_history_spellings_are_ambiguous() -> None:
    history = [
        fd_team("Arsenal"),
        Team(ref=ExternalRef("other", "a"), sport=Sport.FOOTBALL, name="ARSENAL"),
    ]
    report = match_teams([api_team(42, "Arsenal")], history)
    assert report.matched == ()
    assert len(report.ambiguous[0].candidates) == 2


def test_fuzzy_tie_with_duplicate_key_is_ambiguous() -> None:
    history = [
        fd_team("Brighton Hove"),
        Team(ref=ExternalRef("other", "b"), sport=Sport.FOOTBALL, name="BRIGHTON HOVE"),
    ]
    report = match_teams([api_team(51, "Brighton & Hove")], history)
    assert report.matched == ()


def test_empty_history_leaves_everything_unmatched() -> None:
    report = match_teams([api_team(42, "Arsenal")], [])
    assert [team.name for team in report.unmatched] == ["Arsenal"]


def test_alias_keys_are_normalized() -> None:
    assert TEAM_ALIASES["bayern munchen"] == "bayern munich"
    assert TEAM_ALIASES["1 fc koln"] == "fc koln"


def test_identity_map_rewrites_matched_teams_and_tracked_leagues() -> None:
    report = match_teams([api_team(33, "Manchester United"), api_team(36, "Fulham")], HISTORY)
    identity = IdentityMap.from_report(report, LeagueCatalog())
    league = League(ref=ExternalRef("api-football", "39"), sport=Sport.FOOTBALL, name="PL")
    fixture = Fixture(
        ref=ExternalRef("api-football", "1"),
        sport=Sport.FOOTBALL,
        league=league,
        season=2024,
        kickoff=datetime(2024, 8, 16, 19, tzinfo=UTC),
        home=api_team(33, "Manchester United"),
        away=api_team(36, "Fulham"),
    )
    canonical = identity.apply(fixture)
    assert canonical.ref == fixture.ref
    assert canonical.home.ref == ExternalRef("football-data", "Man United")
    assert canonical.away.ref == ExternalRef("api-football", "36")  # unmatched: unchanged
    assert canonical.league.ref == ExternalRef("football-data", "E0")
    assert identity.provider_ref(ExternalRef("football-data", "Man United")) == ExternalRef(
        "api-football", "33"
    )
    assert identity.provider_ref(ExternalRef("football-data", "Fulham")) is None
    assert IdentityMap.from_report(report).leagues == {}
