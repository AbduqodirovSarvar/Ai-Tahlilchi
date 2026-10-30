"""History facts of a fixture: form, venue record, goal averages, H2H and the season table."""

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.db.repo import FixtureRecord
from app.domain import (
    ExternalRef,
    Fixture,
    FixtureResult,
    FixtureStatus,
    League,
    ResultStatus,
    Sport,
    Team,
)
from app.pipeline.match_facts import Record, league_table, match_facts, meetings, record_of

LEAGUE = League(ref=ExternalRef("football-data", "E0"), sport=Sport.FOOTBALL, name="Premier League")
START = datetime(2026, 8, 15, 15, tzinfo=UTC)


def team(name: str) -> Team:
    return Team(ref=ExternalRef("football-data", name), sport=Sport.FOOTBALL, name=name)


def fixture(home: str, away: str, kickoff: datetime) -> Fixture:
    return Fixture(
        ref=ExternalRef("football-data", f"{home}-{away}-{kickoff:%Y%m%d}"),
        sport=Sport.FOOTBALL,
        league=LEAGUE,
        season=2026 if kickoff.month >= 7 else kickoff.year - 1,
        kickoff=kickoff,
        home=team(home),
        away=team(away),
        status=FixtureStatus.FINISHED,
    )


def played(
    number: int, home: str, away: str, score: tuple[int, int] | None, kickoff: datetime
) -> FixtureRecord:
    game = fixture(home, away, kickoff)
    result = (
        None
        if score is None
        else FixtureResult(
            fixture=game.ref,
            status=ResultStatus.FINISHED,
            home_score=score[0],
            away_score=score[1],
        )
    )
    return FixtureRecord(id=number, fixture=game, result=result)


def week(n: int) -> datetime:
    return START + timedelta(weeks=n)


HISTORY = [
    played(1, "Chelsea", "Arsenal", (2, 1), START - timedelta(days=300)),  # last season
    played(2, "Arsenal", "Spurs", (3, 0), week(0)),
    played(3, "Chelsea", "Spurs", (1, 1), week(1)),
    played(4, "Spurs", "Arsenal", (0, 2), week(2)),
    played(5, "Arsenal", "Chelsea", (1, 1), week(3)),
    played(6, "Spurs", "Chelsea", (2, 3), week(4)),
    played(7, "Arsenal", "Fulham", None, week(5)),  # no score: ignored
]


def test_both_sides_get_form_venue_goals_and_table() -> None:
    facts = match_facts(fixture("Arsenal", "Chelsea", week(6)), HISTORY)
    assert facts is not None
    arsenal, chelsea = facts.home, facts.away
    assert arsenal.form == "LWWD"
    assert arsenal.venue == Record(
        played=2, wins=1, draws=1, losses=0, goals_for=4, goals_against=1
    )
    assert arsenal.goals_for_avg == pytest.approx(7 / 4)
    assert arsenal.sample == 4
    assert chelsea.venue.played == 2  # at Arsenal and at Spurs
    assert (arsenal.position, arsenal.points) == (1, 7)
    assert chelsea.position == 2
    assert [m.day for m in facts.meetings] == [week(3).date(), (START - timedelta(days=300)).date()]


def test_only_the_past_counts_and_unknown_teams_have_no_facts() -> None:
    early = match_facts(fixture("Arsenal", "Chelsea", week(0)), HISTORY)
    assert early is not None
    assert early.home.form == "L"  # only last season's defeat at Chelsea came before
    assert early.away.form == "W"
    assert early.home.position is None  # the season had no results yet
    assert match_facts(fixture("Arsenal", "Brentford", week(6)), HISTORY) is None


def test_meetings_are_newest_first_and_capped() -> None:
    many = [played(n, "A", "B", (n % 3, 1), week(n)) for n in range(8)]
    scored = [(record, (n % 3, 1)) for n, record in enumerate(many)]
    found = meetings(fixture("B", "A", week(9)), scored)
    assert len(found) == 5
    assert found[0].day == week(7).date()


def test_table_orders_by_points_then_goal_difference_then_goals() -> None:
    rows = [
        (played(1, "A", "B", (3, 0), week(0)), (3, 0)),
        (played(2, "C", "D", (1, 0), week(0)), (1, 0)),
    ]
    table = league_table(rows)
    assert [table[team(name).ref][0] for name in "ACDB"] == [1, 2, 3, 4]
    assert table[team("D").ref][1:] == (0, 1)


@given(st.lists(st.tuples(st.integers(0, 9), st.integers(0, 9)), max_size=20))
def test_record_totals_add_up(results: list[tuple[int, int]]) -> None:
    record = record_of(results)
    assert record.played == len(results) == record.wins + record.draws + record.losses
    assert record.goals_for == sum(r[0] for r in results)
