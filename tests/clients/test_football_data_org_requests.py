"""Football-Data.org request filters: validation before sending and the exact query strings."""

from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.clients.football_data_org_models import MatchStatus
from app.clients.football_data_org_requests import (
    MAX_LIMIT,
    CompetitionListFilters,
    CompetitionMatchFilters,
    CompetitionTeamsFilters,
    Head2HeadFilters,
    MatchFilters,
    PersonMatchFilters,
    ScorersFilters,
    StandingsFilters,
    StatusFilter,
    TeamMatchFilters,
    Venue,
)

DAY = date(2026, 10, 30)


def test_empty_filters_send_nothing() -> None:
    for filters in (
        CompetitionListFilters(),
        CompetitionMatchFilters(),
        StandingsFilters(),
        ScorersFilters(),
        CompetitionTeamsFilters(),
        TeamMatchFilters(),
        PersonMatchFilters(),
        MatchFilters(),
        Head2HeadFilters(),
    ):
        assert filters.params() == {}


def test_documented_parameter_names_and_formats() -> None:
    assert CompetitionMatchFilters(
        date_from=DAY,
        date_to=date(2026, 11, 2),
        stage="REGULAR_SEASON",
        status=StatusFilter.FINISHED,
        matchday=9,
        group="GROUP_A",
        season=2026,
    ).params() == {
        "dateFrom": "2026-10-30",
        "dateTo": "2026-11-02",
        "stage": "REGULAR_SEASON",
        "status": "FINISHED",
        "matchday": 9,
        "group": "GROUP_A",
        "season": 2026,
    }
    assert StandingsFilters(matchday=3, season=2025, day=DAY).params() == {
        "matchday": 3,
        "season": 2025,
        "date": "2026-10-30",
    }
    assert ScorersFilters(limit=20, season=2025).params() == {"limit": 20, "season": 2025}
    assert TeamMatchFilters(
        competitions=("PL", "CL"), venue=Venue.HOME, status=StatusFilter.SCHEDULED, limit=5
    ).params() == {"competitions": "PL,CL", "venue": "HOME", "status": "SCHEDULED", "limit": 5}
    assert PersonMatchFilters(offset=0, limit=10).params() == {"limit": 10, "offset": 0}
    assert MatchFilters(ids=(3, 1, 2), status=StatusFilter.LIVE).params() == {
        "ids": "3,1,2",
        "status": "LIVE",
    }
    assert MatchFilters(day=DAY, competitions=("PL",)).params() == {
        "date": "2026-10-30",
        "competitions": "PL",
    }
    assert Head2HeadFilters(limit=5).params() == {"limit": 5}
    assert CompetitionListFilters(areas=frozenset({2224, 2072})).params() == {"areas": "2072,2224"}


@pytest.mark.parametrize(
    "build",
    [
        lambda: CompetitionMatchFilters(date_from=DAY),  # dateFrom needs dateTo
        lambda: TeamMatchFilters(date_to=DAY),
        lambda: MatchFilters(date_from=DAY, date_to=date(2026, 10, 1)),  # reversed
        lambda: MatchFilters(day=DAY, date_from=DAY, date_to=DAY),  # either one or the other
        lambda: CompetitionMatchFilters(matchday=0),
        lambda: CompetitionMatchFilters(season=26),  # a starting year, not "26"
        lambda: StandingsFilters(season=3000),
        lambda: ScorersFilters(limit=0),
        lambda: ScorersFilters(limit=MAX_LIMIT + 1),
        lambda: PersonMatchFilters(offset=-1),
        lambda: Head2HeadFilters(limit=-5),
    ],
)
def test_requests_the_provider_would_reject_are_refused_up_front(build: object) -> None:
    with pytest.raises(ValueError, match=r"\w"):
        build()  # type: ignore[operator]


def test_status_filter_of_a_match_status() -> None:
    assert StatusFilter.of(MatchStatus.FINISHED) is StatusFilter.FINISHED
    with pytest.raises(ValueError, match="UNKNOWN"):
        StatusFilter.of(MatchStatus.UNKNOWN)


@given(
    start=st.dates(min_value=date(2000, 1, 1), max_value=date(2100, 1, 1)),
    length=st.integers(min_value=0, max_value=60),
)
def test_a_valid_range_always_renders_iso_dates(start: date, length: int) -> None:
    end = date.fromordinal(start.toordinal() + length)
    params = MatchFilters(date_from=start, date_to=end).params()
    assert date.fromisoformat(str(params["dateFrom"])) == start
    assert date.fromisoformat(str(params["dateTo"])) == end
