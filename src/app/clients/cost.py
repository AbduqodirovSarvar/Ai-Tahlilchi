"""Up-front request cost estimates (plan §9: show the expected API spend before analysis).

Pure arithmetic over a :class:`FetchPlan` that the pipeline fills in from what it intends to
fetch, *after* removing anything already in cache or in the database. Fixtures by id are
fetched in batches of :data:`MAX_IDS_PER_REQUEST`.
"""

from dataclasses import dataclass, fields
from math import ceil
from typing import Final

DEFAULT_ODDS_PAGES_PER_LEAGUE_DAY: Final = 1
MAX_IDS_PER_REQUEST: Final = 20


@dataclass(frozen=True, slots=True, kw_only=True)
class FetchPlan:
    """Counts of the fetches a job intends to make (all non-negative)."""

    league_seasons: int = 0  # fixtures?league&season[&from&to]: 1 each
    teams: int = 0  # fixtures?team&season&from&to: 1 each
    days: int = 0  # fixtures?date: 1 each
    fixtures_by_id: int = 0  # fixtures?ids=a-b-c: ceil(n / 20)
    odds_fixtures: int = 0  # odds?fixture: 1 each
    odds_league_days: int = 0  # odds?league&season&date: pages each
    odds_pages_per_league_day: int = DEFAULT_ODDS_PAGES_PER_LEAGUE_DAY
    injury_fixtures: int = 0  # injuries?fixture: 1 each
    lineup_fixtures: int = 0  # fixtures/lineups?fixture: 1 each
    statistics_fixtures: int = 0  # fixtures/statistics?fixture (xG): 1 each
    # Cap on xG requests (``FootballAdapter.with_expected_goals(max_requests=...)``);
    # ``None`` = uncapped, i.e. one per fixture.
    max_xg_requests: int | None = None

    def __post_init__(self) -> None:
        """Reject negative counts and an empty page estimate."""
        for item in fields(self):
            if (getattr(self, item.name) or 0) < 0:
                msg = f"{item.name} must be >= 0"
                raise ValueError(msg)
        if self.odds_pages_per_league_day < 1:
            msg = "odds_pages_per_league_day must be >= 1"
            raise ValueError(msg)


def estimate_requests(plan: FetchPlan) -> int:
    """Number of provider requests ``plan`` will spend (upper bound for pagination)."""
    return (
        plan.league_seasons
        + plan.teams
        + plan.days
        + ceil(plan.fixtures_by_id / MAX_IDS_PER_REQUEST)
        + plan.odds_fixtures
        + plan.odds_league_days * plan.odds_pages_per_league_day
        + plan.injury_fixtures
        + plan.lineup_fixtures
        + xg_requests(plan)
    )


def xg_requests(plan: FetchPlan) -> int:
    """XG requests ``plan`` will spend: one per fixture, at most ``max_xg_requests``."""
    if plan.max_xg_requests is None:
        return plan.statistics_fixtures
    return min(plan.statistics_fixtures, plan.max_xg_requests)
