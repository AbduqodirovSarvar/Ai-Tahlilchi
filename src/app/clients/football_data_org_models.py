"""Pydantic models of Football-Data.org v4 responses (validated at the boundary).

Not to be confused with ``app.sports.history.football_data`` (football-data.co.uk CSV files):
this is the REST service at ``api.football-data.org``. Field names follow the v4 reference
(``https://docs.football-data.org/general/v4/``); Python names are snake_case with the
provider's camelCase as the alias.

Unknown fields are ignored so additive API changes do not break parsing. The provider is
generous with ``null``: cup teams not drawn yet have no id or name, the free tier sends empty
``lineup``/``bench`` lists and an ``odds`` object without prices (odds are a paid package), so
almost every optional field defaults to "absent".

Wrappers: list endpoints answer ``{"count"/"resultSet", "filters", "<items>": [...]}``;
``GET /matches/{id}``, ``/teams/{id}``, ``/persons/{id}``, ``/areas/{id}`` and
``/competitions/{code}`` return the object itself. Matches inside list wrappers are kept raw
(:data:`RawRecords`) and validated one by one by the client, so one malformed match is logged
and skipped instead of failing the whole response.
"""

from datetime import date
from enum import StrEnum
from typing import Final

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

type RawRecords = tuple[JsonValue, ...]


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


# ------------------------------------------------------------------------------- shared parts
class Area(_Model):
    """A country or region (``2072`` = England)."""

    id: int
    name: str
    code: str | None = None
    flag: str | None = None


class AreaDetail(Area):
    """``GET /areas/{id}``: an area with its parent and children."""

    parent_area_id: int | None = Field(default=None, alias="parentAreaId")
    parent_area: str | None = Field(default=None, alias="parentArea")
    child_areas: tuple[Area, ...] = Field(default=(), alias="childAreas")


class AreaList(_Model):
    """``GET /areas``."""

    count: int = 0
    areas: tuple[Area, ...] = ()


class TeamRef(_Model):
    """A team as other resources reference it."""

    id: int | None = None
    name: str | None = None
    short_name: str | None = Field(default=None, alias="shortName")
    tla: str | None = None
    crest: str | None = None


class Season(_Model):
    """One season of a competition."""

    id: int | None = None
    start_date: date | None = Field(default=None, alias="startDate")
    end_date: date | None = Field(default=None, alias="endDate")
    current_matchday: int | None = Field(default=None, alias="currentMatchday")
    winner: TeamRef | None = None
    stages: tuple[str, ...] = ()

    @property
    def starting_year(self) -> int | None:
        """The ``season`` filter value of this season (2024/25 -> 2024)."""
        return None if self.start_date is None else self.start_date.year


class Competition(_Model):
    """A competition as matches and lists reference it (``code`` is e.g. ``PL``)."""

    id: int
    name: str
    code: str | None = None
    type: str | None = None
    emblem: str | None = None


class CompetitionDetail(Competition):
    """``GET /competitions`` entries and ``GET /competitions/{code}``."""

    area: Area | None = None
    plan: str | None = None
    current_season: Season | None = Field(default=None, alias="currentSeason")
    seasons: tuple[Season, ...] = ()
    number_of_available_seasons: int | None = Field(default=None, alias="numberOfAvailableSeasons")
    last_updated: AwareDatetime | None = Field(default=None, alias="lastUpdated")


class CompetitionList(_Model):
    """``GET /competitions``."""

    count: int = 0
    competitions: tuple[CompetitionDetail, ...] = ()


# --------------------------------------------------------------------------------- people
class Contract(_Model):
    """Start and end month of a contract (``2021-07``)."""

    start: str | None = None
    until: str | None = None


class Person(_Model):
    """A player, coach or referee (``GET /persons/{id}`` and nested references)."""

    id: int | None = None
    name: str | None = None
    first_name: str | None = Field(default=None, alias="firstName")
    last_name: str | None = Field(default=None, alias="lastName")
    date_of_birth: date | None = Field(default=None, alias="dateOfBirth")
    nationality: str | None = None
    position: str | None = None
    shirt_number: int | None = Field(default=None, alias="shirtNumber")
    last_updated: AwareDatetime | None = Field(default=None, alias="lastUpdated")


class PersonDetail(Person):
    """``GET /persons/{id}``: also the club the person plays for now."""

    current_team: TeamRef | None = Field(default=None, alias="currentTeam")


class StaffMember(Person):
    """A coach or squad member of a team, with the contract."""

    contract: Contract | None = None
    market_value: int | None = Field(default=None, alias="marketValue")


class Coach(_Model):
    """The head coach of a team in a match."""

    id: int | None = None
    name: str | None = None
    nationality: str | None = None


class Player(_Model):
    """A starter or a substitute of a match line-up."""

    id: int | None = None
    name: str | None = None
    position: str | None = None
    shirt_number: int | None = Field(default=None, alias="shirtNumber")


# ---------------------------------------------------------------------------------- teams
class Team(TeamRef):
    """``GET /teams/{id}`` and ``GET /competitions/{code}/teams`` entries."""

    area: Area | None = None
    address: str | None = None
    website: str | None = None
    founded: int | None = None
    club_colors: str | None = Field(default=None, alias="clubColors")
    venue: str | None = None
    running_competitions: tuple[Competition, ...] = Field(default=(), alias="runningCompetitions")
    coach: StaffMember | None = None
    squad: tuple[StaffMember, ...] = ()
    staff: tuple[StaffMember, ...] = ()
    last_updated: AwareDatetime | None = Field(default=None, alias="lastUpdated")


class TeamList(_Model):
    """``GET /competitions/{code}/teams``."""

    count: int = 0
    competition: Competition | None = None
    season: Season | None = None
    teams: tuple[Team, ...] = ()


# -------------------------------------------------------------------------------- matches
class MatchTeam(TeamRef):
    """A team in a match; line-up fields are filled by ``GET /matches/{id}`` on paid plans."""

    coach: Coach | None = None
    league_rank: int | None = Field(default=None, alias="leagueRank")
    formation: str | None = None
    lineup: tuple[Player, ...] = ()
    bench: tuple[Player, ...] = ()


class Goals(_Model):
    """Goals of one period; ``None`` before the period is played."""

    home: int | None = None
    away: int | None = None


class Score(_Model):
    """Score by period.

    ``winner`` is ``HOME_TEAM``, ``AWAY_TEAM``, ``DRAW`` or ``None``; ``duration`` is
    ``REGULAR``, ``EXTRA_TIME`` or ``PENALTY_SHOOTOUT``. After extra time ``full_time``
    includes it and ``regular_time`` holds the 90-minute score.
    """

    winner: str | None = None
    duration: str | None = None
    full_time: Goals = Field(default_factory=Goals, alias="fullTime")
    half_time: Goals = Field(default_factory=Goals, alias="halfTime")
    regular_time: Goals | None = Field(default=None, alias="regularTime")

    @property
    def ninety_minutes(self) -> Goals | None:
        """The score after 90 minutes (what markets settle on), ``None`` when unknown."""
        if self.duration in (None, "REGULAR"):
            return self.full_time
        return self.regular_time


class Odds(_Model):
    """Average 1X2 prices; all ``None`` without the provider's odds package."""

    home_win: float | None = Field(default=None, alias="homeWin")
    draw: float | None = None
    away_win: float | None = Field(default=None, alias="awayWin")

    @property
    def available(self) -> bool:
        """Whether all three prices are present."""
        return None not in (self.home_win, self.draw, self.away_win)


class Goal(_Model):
    """A goal (``type`` is ``REGULAR``, ``OWN`` or ``PENALTY``)."""

    minute: int | None = None
    injury_time: int | None = Field(default=None, alias="injuryTime")
    type: str | None = None
    team: TeamRef | None = None
    scorer: Person | None = None
    assist: Person | None = None
    score: Goals | None = None


class Booking(_Model):
    """A card (``card`` is ``YELLOW``, ``YELLOW_RED`` or ``RED``)."""

    minute: int | None = None
    team: TeamRef | None = None
    player: Person | None = None
    card: str | None = None


class Substitution(_Model):
    """A substitution."""

    minute: int | None = None
    team: TeamRef | None = None
    player_out: Person | None = Field(default=None, alias="playerOut")
    player_in: Person | None = Field(default=None, alias="playerIn")


class Referee(_Model):
    """A match official (``type`` is e.g. ``REFEREE``)."""

    id: int | None = None
    name: str | None = None
    type: str | None = None
    nationality: str | None = None


class Match(_Model):
    """One match: teams, status, kick-off, score and, where the plan has them, the details."""

    id: int
    utc_date: AwareDatetime = Field(alias="utcDate")
    status: MatchStatus
    area: Area | None = None
    competition: Competition | None = None
    season: Season | None = None
    minute: int | None = None
    injury_time: int | None = Field(default=None, alias="injuryTime")
    attendance: int | None = None
    venue: str | None = None
    matchday: int | None = None
    stage: str | None = None
    group: str | None = None
    last_updated: AwareDatetime | None = Field(default=None, alias="lastUpdated")
    home_team: MatchTeam = Field(alias="homeTeam")
    away_team: MatchTeam = Field(alias="awayTeam")
    score: Score = Field(default_factory=Score)
    goals: tuple[Goal, ...] = ()
    bookings: tuple[Booking, ...] = ()
    substitutions: tuple[Substitution, ...] = ()
    odds: Odds = Field(default_factory=Odds)
    referees: tuple[Referee, ...] = ()


class MatchListWrapper(_Model):
    """The envelope of every match list (records validated separately)."""

    matches: RawRecords


class Head2HeadSide(_Model):
    """One team's record in the earlier meetings."""

    id: int | None = None
    name: str | None = None
    wins: int = 0
    draws: int = 0
    losses: int = 0


class Head2HeadAggregates(_Model):
    """Totals over the earlier meetings."""

    number_of_matches: int = Field(default=0, alias="numberOfMatches")
    total_goals: int = Field(default=0, alias="totalGoals")
    home_team: Head2HeadSide | None = Field(default=None, alias="homeTeam")
    away_team: Head2HeadSide | None = Field(default=None, alias="awayTeam")


class Head2HeadWrapper(_Model):
    """``GET /matches/{id}/head2head`` envelope (matches validated separately)."""

    aggregates: Head2HeadAggregates = Field(default_factory=Head2HeadAggregates)
    matches: RawRecords = ()


class Head2Head(_Model):
    """Earlier meetings of the two teams of a match and their totals."""

    aggregates: Head2HeadAggregates
    matches: tuple[Match, ...]


# ------------------------------------------------------------------------ standings, scorers
class TableRow(_Model):
    """One line of a league table."""

    position: int
    team: TeamRef
    played_games: int = Field(default=0, alias="playedGames")
    form: str | None = None
    won: int = 0
    draw: int = 0
    lost: int = 0
    points: int = 0
    goals_for: int = Field(default=0, alias="goalsFor")
    goals_against: int = Field(default=0, alias="goalsAgainst")
    goal_difference: int = Field(default=0, alias="goalDifference")


class Standing(_Model):
    """One table: ``type`` is ``TOTAL``, ``HOME`` or ``AWAY``; ``group`` for group stages."""

    stage: str | None = None
    type: str | None = None
    group: str | None = None
    table: tuple[TableRow, ...] = ()


class Standings(_Model):
    """``GET /competitions/{code}/standings``."""

    area: Area | None = None
    competition: Competition | None = None
    season: Season | None = None
    standings: tuple[Standing, ...] = ()

    def table(self, kind: str = "TOTAL") -> tuple[TableRow, ...]:
        """The rows of the first table of ``kind`` (empty when the provider has none)."""
        return next((item.table for item in self.standings if item.type == kind), ())


class Scorer(_Model):
    """A top scorer."""

    player: Person
    team: TeamRef | None = None
    played_matches: int | None = Field(default=None, alias="playedMatches")
    goals: int = 0
    assists: int | None = None
    penalties: int | None = None


class Scorers(_Model):
    """``GET /competitions/{code}/scorers``."""

    count: int = 0
    competition: Competition | None = None
    season: Season | None = None
    scorers: tuple[Scorer, ...] = ()


class ErrorBody(_Model):
    """An error answer; the reference documents ``error``, the live API also ``message``."""

    error: str | None = None
    message: str | None = None
    error_code: int | None = Field(default=None, alias="errorCode")

    @property
    def text(self) -> str | None:
        """The provider's explanation, whichever field carries it."""
        return self.message or self.error


# --------------------------------------------------------------------------- line-up helpers
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


FREE_TIER_COMPETITIONS: Final = (
    "WC",
    "CL",
    "BL1",
    "DED",
    "BSA",
    "PD",
    "FL1",
    "ELC",
    "PPL",
    "EC",
    "SA",
    "PL",
)
"""Competitions of the free plan (TIER_ONE): ``GET /competitions`` lists what a key can see."""
