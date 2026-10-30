"""Data matching engine: Football-Data.org matches to football-data.co.uk rows."""

from datetime import date
from typing import Any

from app.clients.football_data_org_models import Match
from app.sports.aggregator.matching import MatchingEngine, csv_day
from app.sports.aggregator.models import LinkStatus, MatchOdds, OddsRow

ODDS = MatchOdds(home_win=2.1, draw=3.4, away_win=3.3)
SEASON_TEAMS = ("Man United", "Man City", "Arsenal", "Chelsea", "Tottenham", "Wolves")


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


def row(
    home: str, away: str, day: date = date(2026, 10, 31), odds: MatchOdds | None = ODDS
) -> OddsRow:
    return OddsRow(division="E0", day=day, home_team=home, away_team=away, odds=odds)


def season_rows() -> list[OddsRow]:
    """Played games: they give the engine every team spelling of the division."""
    pairs = zip(SEASON_TEAMS, SEASON_TEAMS[1:] + SEASON_TEAMS[:1], strict=True)
    return [row(home, away, date(2026, 8, 22)) for home, away in pairs]


def test_official_names_meet_csv_spellings_on_the_same_day() -> None:
    derby = match(1, (66, "Manchester United FC"), (65, "Manchester City FC"))
    wolves = match(2, (76, "Wolverhampton Wanderers FC"), (73, "Tottenham Hotspur FC"))
    rows = [*season_rows(), row("Man United", "Man City"), row("Wolves", "Tottenham")]
    links = MatchingEngine().link([derby, wolves], rows)
    assert links[1].status is LinkStatus.MATCHED
    assert links[1].row == row("Man United", "Man City")
    assert links[2].status is LinkStatus.MATCHED


def test_home_and_away_are_not_swapped() -> None:
    reverse = match(3, (65, "Manchester City FC"), (66, "Manchester United FC"))
    links = MatchingEngine().link([reverse], [*season_rows(), row("Man United", "Man City")])
    assert links[3].status is LinkStatus.NO_ROW


def test_a_day_of_slack_and_the_closest_row_wins() -> None:
    game = match(4, (57, "Arsenal FC"), (61, "Chelsea FC"))
    near = row("Arsenal", "Chelsea", date(2026, 11, 1), MatchOdds(1.9, 3.5, 4.0))
    exact = row("Arsenal", "Chelsea", date(2026, 10, 31))
    links = MatchingEngine().link([game], [*season_rows(), near, exact])
    assert links[4].row == exact
    far = row("Arsenal", "Chelsea", date(2026, 11, 3))
    assert MatchingEngine().link([game], [*season_rows(), far])[4].status is LinkStatus.NO_ROW


def test_dates_are_compared_on_the_uk_calendar() -> None:
    late = match(5, (57, "Arsenal FC"), (61, "Chelsea FC"), utcDate="2026-07-31T23:30:00Z")
    assert csv_day(late) == date(2026, 8, 1)  # 00:30 in London (summer time)
    winter = match(6, (57, "Arsenal FC"), (61, "Chelsea FC"), utcDate="2026-12-31T23:30:00Z")
    assert csv_day(winter) == date(2026, 12, 31)


def test_a_row_without_prices_is_found_but_has_no_odds() -> None:
    game = match(7, (57, "Arsenal FC"), (61, "Chelsea FC"))
    link = MatchingEngine().link([game], [*season_rows(), row("Arsenal", "Chelsea", odds=None)])[7]
    assert link.status is LinkStatus.NO_ODDS
    assert link.row is not None


def test_unknown_or_undrawn_teams_are_never_guessed() -> None:
    stranger = match(8, (999, "Nowhere Rovers FC"), (61, "Chelsea FC"))
    undrawn = match(9, (57, "Arsenal FC"), (61, "Chelsea FC"), homeTeam={"id": None, "name": None})
    links = MatchingEngine().link([stranger, undrawn], season_rows())
    assert links[8].status is LinkStatus.TEAMS_UNMATCHED
    assert links[9].status is LinkStatus.TEAMS_UNMATCHED


def test_no_rows_or_no_matches() -> None:
    game = match(10, (57, "Arsenal FC"), (61, "Chelsea FC"))
    assert MatchingEngine().link([game], [])[10].status is LinkStatus.TEAMS_UNMATCHED
    assert MatchingEngine().link([], season_rows()) == {}


def test_custom_aliases_reach_the_matcher() -> None:
    game = match(11, (1, "Gamma Union"), (61, "Chelsea FC"))
    rows = [*season_rows(), row("Zeta", "Chelsea")]
    assert MatchingEngine().link([game], rows)[11].status is LinkStatus.TEAMS_UNMATCHED
    engine = MatchingEngine(aliases={"gamma union": "zeta"})
    assert engine.link([game], rows)[11].status is LinkStatus.MATCHED
