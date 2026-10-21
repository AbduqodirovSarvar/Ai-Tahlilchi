"""Pydantic models of API-Football v3 responses (validated at the boundary).

Only the fields the system uses are declared; unknown fields are ignored so additive API
changes do not break parsing. Every response is wrapped in the same envelope::

    {
        "get": "fixtures",
        "parameters": {...},
        "errors": [] | {...},
        "results": 10,
        "paging": {"current": 1, "total": 1},
        "response": [...],
    }

Records inside ``response`` are validated one by one by the client, so one malformed
record is logged and skipped instead of failing the whole response.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


class Paging(_Model):
    """Page position of a paginated response (``odds``, ``injuries``)."""

    current: int = 1
    total: int = 1


class Envelope(_Model):
    """The common response wrapper; ``response`` records are validated separately."""

    get: str = ""
    errors: dict[str, str] | list[JsonValue] = Field(default_factory=list)
    results: int = 0
    paging: Paging = Field(default_factory=Paging)
    # A list of records for every data endpoint; one object for ``/status``.
    response: list[JsonValue] | dict[str, JsonValue] = Field(default_factory=list)
    # Set by the client, not part of the provider body: when the provider answered.
    fetched_at: datetime | None = Field(default=None, exclude=True)

    @property
    def records(self) -> list[JsonValue]:
        """``response`` as a list of records (an object response is one record)."""
        return [self.response] if isinstance(self.response, dict) else list(self.response)

    @property
    def error_messages(self) -> dict[str, str]:
        """Provider-reported errors (sent with HTTP 200) as ``{field: message}``."""
        if isinstance(self.errors, dict):
            return dict(self.errors)
        return {str(index): str(error) for index, error in enumerate(self.errors)}


class StatusInfo(_Model):
    """Fixture status; ``short`` is the code (``NS``, ``FT``, ``PST``...)."""

    short: str
    long: str | None = None
    elapsed: int | None = None


class FixtureInfo(_Model):
    """Identity, kick-off and status of a fixture."""

    id: int
    date: datetime
    status: StatusInfo


class LeagueInfo(_Model):
    """League and season of a fixture."""

    id: int
    name: str
    country: str | None = None
    season: int
    round: str | None = None


class TeamInfo(_Model):
    """A team reference."""

    id: int
    name: str


class Teams(_Model):
    """Home and away teams."""

    home: TeamInfo
    away: TeamInfo


class ScorePair(_Model):
    """Home and away goals (``None`` until known)."""

    home: int | None = None
    away: int | None = None


class Score(_Model):
    """Scores by period. ``fulltime`` is the score after regular time (no extra time)."""

    halftime: ScorePair = Field(default_factory=ScorePair)
    fulltime: ScorePair = Field(default_factory=ScorePair)
    extratime: ScorePair = Field(default_factory=ScorePair)
    penalty: ScorePair = Field(default_factory=ScorePair)


class FixtureItem(_Model):
    """One record of ``GET /fixtures``."""

    fixture: FixtureInfo
    league: LeagueInfo
    teams: Teams
    goals: ScorePair = Field(default_factory=ScorePair)
    score: Score = Field(default_factory=Score)


class OddValue(_Model):
    """One price: ``value`` names the selection (``Home``, ``Over 2.5``, ``Home -0.5``).

    ``odd`` is kept as sent (a decimal string, possibly ``null``): one unparsable price
    must only drop that price during mapping, not the whole fixture record.
    """

    value: str | int | float
    odd: str | float | None = None


class Bet(_Model):
    """One market of one bookmaker (``Match Winner``, ``Goals Over/Under``...)."""

    id: int
    name: str
    values: list[OddValue] = Field(default_factory=list)


class Bookmaker(_Model):
    """A bookmaker and its markets."""

    id: int
    name: str
    bets: list[Bet] = Field(default_factory=list)


class FixtureRef(_Model):
    """Fixture reference inside ``odds``/``injuries`` records."""

    id: int
    date: datetime | None = None


class OddsLeague(_Model):
    """League reference inside ``odds`` records."""

    id: int
    season: int


class OddsItem(_Model):
    """One record of ``GET /odds`` (all bookmakers of one fixture)."""

    league: OddsLeague
    fixture: FixtureRef
    update: datetime | None = None
    bookmakers: list[Bookmaker] = Field(default_factory=list)
    # Set by the client, not part of the provider body: when this page was fetched.
    fetched_at: datetime | None = Field(default=None, exclude=True)


class InjuryPlayer(_Model):
    """A player missing (``Missing Fixture``) or doubtful (``Questionable``) for a fixture."""

    id: int | None = None
    name: str
    type: str | None = None
    reason: str | None = None


class InjuryItem(_Model):
    """One record of ``GET /injuries``."""

    player: InjuryPlayer
    team: TeamInfo
    fixture: FixtureRef


class LineupPlayer(_Model):
    """A player in a line-up."""

    id: int | None = None
    name: str
    number: int | None = None
    pos: str | None = None


class LineupEntry(_Model):
    """Wrapper object used by the line-up lists."""

    player: LineupPlayer


class LineupItem(_Model):
    """One team's line-up from ``GET /fixtures/lineups``."""

    team: TeamInfo
    formation: str | None = None
    start_xi: list[LineupEntry] = Field(default_factory=list, alias="startXI")
    substitutes: list[LineupEntry] = Field(default_factory=list)


class StatisticEntry(_Model):
    """One statistic (``expected_goals`` carries xG as a decimal string)."""

    type: str
    value: str | int | float | None = None


class TeamStatisticsItem(_Model):
    """One team's statistics from ``GET /fixtures/statistics``."""

    team: TeamInfo
    statistics: list[StatisticEntry] = Field(default_factory=list)
