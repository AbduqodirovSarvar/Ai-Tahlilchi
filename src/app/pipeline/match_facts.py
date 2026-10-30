"""Pre-match facts of a fixture from the stored history: form, venue record, goals, H2H, table.

They ground the analyst's short reasons (the LLM may only cite these numbers, and the template
fallback words them directly). Everything is read from the database, so it costs no provider
request. Only matches finished before the fixture's kick-off count.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final

from app.db.repo import FixtureRecord, finished_fixtures
from app.domain import ExternalRef, Fixture, Team
from app.pipeline.context import PipelineContext
from app.pipeline.form import FORM_LENGTH, result_letter, score_of
from app.sports.football_leagues import season_for

GOALS_SAMPLE: Final = 10
VENUE_SAMPLE: Final = 5
MEETINGS_LIMIT: Final = 5
# Head-to-head needs several seasons; the table and form only the current one.
HISTORY_LOOKBACK: Final = timedelta(days=3 * 365)
SEASON_FIRST_MONTH: Final = 7
OVER_LINE: Final = 2.5
POINTS_WIN: Final = 3


@dataclass(frozen=True, slots=True)
class Record:
    """Wins, draws, losses and goals over some matches of one team."""

    played: int = 0
    wins: int = 0
    draws: int = 0
    losses: int = 0
    goals_for: int = 0
    goals_against: int = 0


@dataclass(frozen=True, slots=True)
class TeamFacts:
    """One side's recent picture (``position`` is ``None`` without a season table)."""

    form: str  # last results, oldest first, e.g. "WDLWW"
    venue: Record  # the home side's home matches, the away side's away matches
    goals_for_avg: float
    goals_against_avg: float
    over_2_5_share: float
    btts_share: float
    sample: int  # matches behind the averages and shares
    position: int | None = None
    points: int | None = None
    table_played: int = 0


@dataclass(frozen=True, slots=True)
class Meeting:
    """A previous meeting of the two teams."""

    day: date
    home: str
    away: str
    home_goals: int
    away_goals: int


@dataclass(frozen=True, slots=True)
class MatchFacts:
    """Facts of both sides and their last meetings, newest first."""

    home: TeamFacts
    away: TeamFacts
    meetings: tuple[Meeting, ...] = ()


async def read_facts(
    ctx: PipelineContext, fixtures: Mapping[int, Fixture]
) -> dict[int, MatchFacts]:
    """Facts of ``fixtures`` (canonical teams), keyed like the input; one query per league.

    A team without any finished match is left out (no facts for that fixture).
    """
    by_league: defaultdict[ExternalRef, dict[int, Fixture]] = defaultdict(dict)
    for key, fixture in fixtures.items():
        by_league[ctx.leagues.canonical(fixture.league.ref)][key] = fixture
    facts: dict[int, MatchFacts] = {}
    for league, group in by_league.items():
        kickoffs = [fixture.kickoff for fixture in group.values()]
        async with ctx.sessions() as session:
            history = await finished_fixtures(
                session,
                leagues=ctx.leagues.group(league),
                start=min(kickoffs) - HISTORY_LOOKBACK,
                end=max(kickoffs),
                preferred_sources=ctx.config.history_sources,
            )
        for key, fixture in group.items():
            found = match_facts(fixture, history)
            if found is not None:
                facts[key] = found
    return facts


def match_facts(fixture: Fixture, history: Sequence[FixtureRecord]) -> MatchFacts | None:
    """The facts of ``fixture`` from ``history`` (oldest first); ``None`` if a side is unseen."""
    before = [
        (record, score)
        for record in history
        if record.fixture.kickoff < fixture.kickoff and (score := score_of(record)) is not None
    ]
    table = league_table(
        (record, score)
        for record, score in before
        if record.fixture.kickoff >= _season_start(fixture)
    )
    home = team_facts(fixture.home, before, at_home=True, table=table)
    away = team_facts(fixture.away, before, at_home=False, table=table)
    if home is None or away is None:
        return None
    return MatchFacts(home=home, away=away, meetings=meetings(fixture, before))


type Scored = tuple[FixtureRecord, tuple[int, int]]
type Table = Mapping[ExternalRef, tuple[int, int, int]]  # position, points, played


def team_facts(
    team: Team, history: Sequence[Scored], *, at_home: bool, table: Table
) -> TeamFacts | None:
    """``team``'s facts; ``at_home`` picks which venue record is kept."""
    own = [_perspective(team, record, score) for record, score in history]
    played = [entry for entry in own if entry is not None]
    if not played:
        return None
    recent = played[-GOALS_SAMPLE:]
    venue = [goals for goals, home in played if home is at_home][-VENUE_SAMPLE:]
    standing = table.get(team.ref)
    return TeamFacts(
        form="".join(result_letter(*goals) for goals, _ in played[-FORM_LENGTH:]),
        venue=record_of(venue),
        goals_for_avg=sum(g[0] for g, _ in recent) / len(recent),
        goals_against_avg=sum(g[1] for g, _ in recent) / len(recent),
        over_2_5_share=sum(sum(g) > OVER_LINE for g, _ in recent) / len(recent),
        btts_share=sum(min(g) > 0 for g, _ in recent) / len(recent),
        sample=len(recent),
        position=None if standing is None else standing[0],
        points=None if standing is None else standing[1],
        table_played=0 if standing is None else standing[2],
    )


def _perspective(
    team: Team, record: FixtureRecord, score: tuple[int, int]
) -> tuple[tuple[int, int], bool] | None:
    """``((goals for, goals against), played at home)`` or ``None`` if ``team`` did not play."""
    if record.fixture.home.ref == team.ref:
        return score, True
    if record.fixture.away.ref == team.ref:
        return (score[1], score[0]), False
    return None


def record_of(results: Iterable[tuple[int, int]]) -> Record:
    """Totals of ``(goals for, goals against)`` pairs."""
    wins = draws = losses = goals_for = goals_against = played = 0
    for scored, conceded in results:
        played += 1
        goals_for += scored
        goals_against += conceded
        wins += scored > conceded
        draws += scored == conceded
        losses += scored < conceded
    return Record(played, wins, draws, losses, goals_for, goals_against)


def league_table(history: Iterable[Scored]) -> dict[ExternalRef, tuple[int, int, int]]:
    """``team -> (position, points, played)``: points, then goal difference, then goals."""
    totals: defaultdict[ExternalRef, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    for record, (home_goals, away_goals) in history:
        for ref, scored, conceded in (
            (record.fixture.home.ref, home_goals, away_goals),
            (record.fixture.away.ref, away_goals, home_goals),
        ):
            row = totals[ref]
            row[0] += POINTS_WIN if scored > conceded else int(scored == conceded)
            row[1] += scored - conceded
            row[2] += scored
            row[3] += 1
    ordered = sorted(totals.items(), key=lambda item: (-item[1][0], -item[1][1], -item[1][2]))
    return {ref: (place, row[0], row[3]) for place, (ref, row) in enumerate(ordered, start=1)}


def meetings(fixture: Fixture, history: Sequence[Scored]) -> tuple[Meeting, ...]:
    """The last :data:`MEETINGS_LIMIT` meetings of the two teams, either venue, newest first."""
    pair = {fixture.home.ref, fixture.away.ref}
    found = [
        Meeting(
            day=record.fixture.kickoff.date(),
            home=record.fixture.home.name,
            away=record.fixture.away.name,
            home_goals=score[0],
            away_goals=score[1],
        )
        for record, score in history
        if {record.fixture.home.ref, record.fixture.away.ref} == pair
    ]
    return tuple(reversed(found[-MEETINGS_LIMIT:]))


def _season_start(fixture: Fixture) -> datetime:
    season = season_for(fixture.kickoff.date())
    return fixture.kickoff.replace(
        year=season, month=SEASON_FIRST_MONTH, day=1, hour=0, minute=0, second=0, microsecond=0
    )
