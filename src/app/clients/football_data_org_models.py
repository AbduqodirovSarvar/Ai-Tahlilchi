"""Pydantic models of Football-Data.org v4 responses (validated at the boundary).

Not to be confused with ``app.sports.history.football_data`` (football-data.co.uk CSV files):
this is the REST service at ``api.football-data.org``.

Only the fields the system uses are declared; unknown fields are ignored so additive API
changes do not break parsing. The provider is generous with ``null``: teams of a cup draw that
is not made yet have no id or name, and the free tier sends empty ``lineup``/``bench`` lists
(line-ups are a paid feature), so almost every optional field here defaults to "absent".

List endpoints wrap matches as ``{"filters": {...}, "resultSet": {...}, "matches": [...]}``;
``GET /matches/{id}`` returns the match object itself. The client validates list records one
by one, so one malformed match is logged and skipped instead of failing the whole response.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


class MatchStatus(StrEnum):
    """Match status as the provider spells it; an unseen value becomes ``UNKNOWN``."""

    SCHEDULED = "SCHEDULED"
    TIMED = "TIMED"
    IN_PLAY = "IN_PLAY"
    PAUSED = "PAUSED"
    EXTRA_TIME = "EXTRA_TIME"
    PENALTY_SHOOTOUT = "PENALTY_SHOOTOUT"
    FINISHED = "FINISHED"
    SUSPENDED = "SUSPENDED"
    POSTPONED = "POSTPONED"
    CANCELLED = "CANCELLED"
    AWARDED = "AWARDED"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def _missing_(cls, value: object) -> "MatchStatus":  # noqa: ARG003 - fixed by Enum protocol
        """A status added by the provider later must not make the whole match unreadable."""
        return cls.UNKNOWN


class Coach(_Model):
    """The head coach of a team in a match."""

    id: int | None = None
    name: str | None = None
    nationality: str | None = None


class Player(_Model):
    """A starter or a substitute: name, position and shirt number."""

    id: int | None = None
    name: str | None = None
    position: str | None = None
    shirt_number: int | None = Field(default=None, alias="shirtNumber")


class Competition(_Model):
    """The competition a match belongs to (``code`` is e.g. ``PL``)."""

    id: int
    name: str
    code: str | None = None


class MatchTeam(_Model):
    """A team in a match; line-up fields are filled by ``GET /matches/{id}`` on paid plans."""

    id: int | None = None
    name: str | None = None
    short_name: str | None = Field(default=None, alias="shortName")
    tla: str | None = None
    coach: Coach | None = None
    formation: str | None = None
    lineup: tuple[Player, ...] = ()
    bench: tuple[Player, ...] = ()


class Goals(_Model):
    """Goals of one period; ``None`` before the period is played."""

    home: int | None = None
    away: int | None = None


class Score(_Model):
    """Score by period; ``full_time`` includes extra time but not a penalty shoot-out."""

    winner: str | None = None
    duration: str | None = None
    full_time: Goals = Field(default_factory=Goals, alias="fullTime")
    half_time: Goals = Field(default_factory=Goals, alias="halfTime")


class Match(_Model):
    """One match: teams, status, kick-off, score and (when the plan has them) line-ups."""

    id: int
    utc_date: datetime = Field(alias="utcDate")
    status: MatchStatus
    matchday: int | None = None
    stage: str | None = None
    competition: Competition | None = None
    home_team: MatchTeam = Field(alias="homeTeam")
    away_team: MatchTeam = Field(alias="awayTeam")
    score: Score = Field(default_factory=Score)


class TeamLineup(_Model):
    """One side's starting eleven, bench and coach, ready to show."""

    team_id: int | None
    team_name: str | None
    formation: str | None
    coach: Coach | None
    starters: tuple[Player, ...]
    bench: tuple[Player, ...]

    @property
    def announced(self) -> bool:
        """Whether a starting eleven is known (empty on the free plan and before kick-off)."""
        return bool(self.starters)


class MatchLineups(_Model):
    """Both sides' line-ups of one match."""

    match_id: int
    status: MatchStatus
    home: TeamLineup
    away: TeamLineup

    @property
    def announced(self) -> bool:
        """Whether both starting elevens are known."""
        return self.home.announced and self.away.announced


def team_lineup(team: MatchTeam) -> TeamLineup:
    """The line-up part of ``team``."""
    return TeamLineup(
        team_id=team.id,
        team_name=team.name,
        formation=team.formation,
        coach=team.coach,
        starters=team.lineup,
        bench=team.bench,
    )


def match_lineups(match: Match) -> MatchLineups:
    """Both line-ups of ``match``."""
    return MatchLineups(
        match_id=match.id,
        status=match.status,
        home=team_lineup(match.home_team),
        away=team_lineup(match.away_team),
    )
