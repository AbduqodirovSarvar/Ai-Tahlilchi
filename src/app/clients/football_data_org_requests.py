"""Typed request filters of Football-Data.org v4 (one frozen class per endpoint family).

Each filter validates itself on construction, so a request the provider would answer with
``400 Bad Request`` (a filter in the wrong format, ``dateFrom`` without ``dateTo``) is refused
before it is sent and costs nothing. :meth:`params` turns it into the query string the API
documents (``https://docs.football-data.org/general/v4/``):

=========================================  ===============================================
Endpoint                                   Filters
=========================================  ===============================================
``GET /areas``, ``/areas/{id}``            none
``GET /competitions``                      ``areas``
``GET /competitions/{code}/matches``       ``dateFrom``, ``dateTo``, ``stage``, ``status``,
                                           ``matchday``, ``group``, ``season``
``GET /competitions/{code}/standings``     ``matchday``, ``season``, ``date``
``GET /competitions/{code}/scorers``       ``limit``, ``season``
``GET /competitions/{code}/teams``         ``season``
``GET /teams/{id}/matches``                ``dateFrom``, ``dateTo``, ``season``,
                                           ``competitions``, ``status``, ``venue``, ``limit``
``GET /persons/{id}/matches``              ``dateFrom``, ``dateTo``, ``status``,
                                           ``competitions``, ``limit``, ``offset``
``GET /matches``                           ``ids``, ``date``, ``dateFrom``, ``dateTo``,
                                           ``status``, ``competitions``
``GET /matches/{id}/head2head``            ``limit``, ``dateFrom``, ``dateTo``, ``competitions``
=========================================  ===============================================

Dates are ``yyyy-MM-dd`` (UTC days), a season is its starting year (2024/25 -> ``2024``),
lists are comma-separated.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Final

from app.clients.football_data_org_models import MatchStatus

MIN_SEASON: Final = 1900
MAX_SEASON: Final = 2200
MAX_LIMIT: Final = 500

type QueryParams = dict[str, str | int]


class Venue(StrEnum):
    """Home or away, for a team's matches."""

    HOME = "HOME"
    AWAY = "AWAY"


class StatusFilter(StrEnum):
    """Status values a filter accepts: the match statuses plus ``LIVE`` (in play or paused)."""

    SCHEDULED = "SCHEDULED"
    TIMED = "TIMED"
    LIVE = "LIVE"
    IN_PLAY = "IN_PLAY"
    PAUSED = "PAUSED"
    FINISHED = "FINISHED"
    POSTPONED = "POSTPONED"
    SUSPENDED = "SUSPENDED"
    CANCELLED = "CANCELLED"
    AWARDED = "AWARDED"

    @classmethod
    def of(cls, status: MatchStatus) -> "StatusFilter":
        """The filter value of a match status (``UNKNOWN`` has none).

        Raises:
            ValueError: ``status`` cannot be filtered on.
        """
        return cls(status.value)


def _check_dates(date_from: date | None, date_to: date | None) -> None:
    if (date_from is None) != (date_to is None):
        msg = "dateFrom and dateTo must be given together"
        raise ValueError(msg)
    if date_from is not None and date_to is not None and date_to < date_from:
        msg = f"dateTo {date_to} is before dateFrom {date_from}"
        raise ValueError(msg)


def _check_positive(value: int | None, name: str, upper: int | None = None) -> None:
    if value is None:
        return
    if value < 1 or (upper is not None and value > upper):
        bound = f"1..{upper}" if upper is not None else ">= 1"
        msg = f"{name} must be {bound} (got {value})"
        raise ValueError(msg)


def _check_season(season: int | None) -> None:
    if season is not None and not MIN_SEASON <= season <= MAX_SEASON:
        msg = f"season must be a starting year {MIN_SEASON}..{MAX_SEASON} (got {season})"
        raise ValueError(msg)


def _joined(values: Iterable[object]) -> str:
    return ",".join(str(value) for value in values)


def _put(params: QueryParams, name: str, value: object) -> None:
    """Add ``value`` under ``name`` unless it is ``None`` or an empty collection."""
    if value is None:
        return
    if isinstance(value, tuple | frozenset):
        if value:
            params[name] = _joined(sorted(value) if isinstance(value, frozenset) else value)
        return
    if isinstance(value, date):
        params[name] = value.isoformat()
    elif isinstance(value, StrEnum):
        params[name] = value.value
    elif isinstance(value, int | str):
        params[name] = value


@dataclass(frozen=True, slots=True, kw_only=True)
class CompetitionListFilters:
    """``GET /competitions``: ``areas`` = area ids (empty = every competition of the plan)."""

    areas: frozenset[int] = frozenset()

    def params(self) -> QueryParams:
        """The query string."""
        params: QueryParams = {}
        _put(params, "areas", self.areas)
        return params


@dataclass(frozen=True, slots=True, kw_only=True)
class CompetitionMatchFilters:
    """``GET /competitions/{code}/matches``."""

    date_from: date | None = None
    date_to: date | None = None
    stage: str | None = None
    status: StatusFilter | None = None
    matchday: int | None = None
    group: str | None = None
    season: int | None = None

    def __post_init__(self) -> None:
        """Refuse what the provider would answer with 400."""
        _check_dates(self.date_from, self.date_to)
        _check_positive(self.matchday, "matchday")
        _check_season(self.season)

    def params(self) -> QueryParams:
        """The query string."""
        params: QueryParams = {}
        _put(params, "dateFrom", self.date_from)
        _put(params, "dateTo", self.date_to)
        _put(params, "stage", self.stage)
        _put(params, "status", self.status)
        _put(params, "matchday", self.matchday)
        _put(params, "group", self.group)
        _put(params, "season", self.season)
        return params


@dataclass(frozen=True, slots=True, kw_only=True)
class StandingsFilters:
    """``GET /competitions/{code}/standings``: the table after a matchday, season or day."""

    matchday: int | None = None
    season: int | None = None
    day: date | None = None

    def __post_init__(self) -> None:
        """Refuse what the provider would answer with 400."""
        _check_positive(self.matchday, "matchday")
        _check_season(self.season)

    def params(self) -> QueryParams:
        """The query string."""
        params: QueryParams = {}
        _put(params, "matchday", self.matchday)
        _put(params, "season", self.season)
        _put(params, "date", self.day)
        return params


@dataclass(frozen=True, slots=True, kw_only=True)
class ScorersFilters:
    """``GET /competitions/{code}/scorers`` (the provider's default limit is 10)."""

    limit: int | None = None
    season: int | None = None

    def __post_init__(self) -> None:
        """Refuse what the provider would answer with 400."""
        _check_positive(self.limit, "limit", MAX_LIMIT)
        _check_season(self.season)

    def params(self) -> QueryParams:
        """The query string."""
        params: QueryParams = {}
        _put(params, "limit", self.limit)
        _put(params, "season", self.season)
        return params


@dataclass(frozen=True, slots=True, kw_only=True)
class CompetitionTeamsFilters:
    """``GET /competitions/{code}/teams``."""

    season: int | None = None

    def __post_init__(self) -> None:
        """Refuse what the provider would answer with 400."""
        _check_season(self.season)

    def params(self) -> QueryParams:
        """The query string."""
        params: QueryParams = {}
        _put(params, "season", self.season)
        return params


@dataclass(frozen=True, slots=True, kw_only=True)
class TeamMatchFilters:
    """``GET /teams/{id}/matches``."""

    date_from: date | None = None
    date_to: date | None = None
    season: int | None = None
    competitions: tuple[str, ...] = ()
    status: StatusFilter | None = None
    venue: Venue | None = None
    limit: int | None = None

    def __post_init__(self) -> None:
        """Refuse what the provider would answer with 400."""
        _check_dates(self.date_from, self.date_to)
        _check_season(self.season)
        _check_positive(self.limit, "limit", MAX_LIMIT)

    def params(self) -> QueryParams:
        """The query string."""
        params: QueryParams = {}
        _put(params, "dateFrom", self.date_from)
        _put(params, "dateTo", self.date_to)
        _put(params, "season", self.season)
        _put(params, "competitions", self.competitions)
        _put(params, "status", self.status)
        _put(params, "venue", self.venue)
        _put(params, "limit", self.limit)
        return params


@dataclass(frozen=True, slots=True, kw_only=True)
class PersonMatchFilters:
    """``GET /persons/{id}/matches``."""

    date_from: date | None = None
    date_to: date | None = None
    status: StatusFilter | None = None
    competitions: tuple[str, ...] = ()
    limit: int | None = None
    offset: int | None = None

    def __post_init__(self) -> None:
        """Refuse what the provider would answer with 400."""
        _check_dates(self.date_from, self.date_to)
        _check_positive(self.limit, "limit", MAX_LIMIT)
        if self.offset is not None and self.offset < 0:
            msg = f"offset must be >= 0 (got {self.offset})"
            raise ValueError(msg)

    def params(self) -> QueryParams:
        """The query string."""
        params: QueryParams = {}
        _put(params, "dateFrom", self.date_from)
        _put(params, "dateTo", self.date_to)
        _put(params, "status", self.status)
        _put(params, "competitions", self.competitions)
        _put(params, "limit", self.limit)
        _put(params, "offset", self.offset)
        return params


@dataclass(frozen=True, slots=True, kw_only=True)
class MatchFilters:
    """``GET /matches``: matches across every competition of the plan.

    ``ids`` alone selects matches by id; ``day`` one UTC day; ``date_from``/``date_to`` a range
    (the client splits ranges longer than the provider accepts).
    """

    ids: tuple[int, ...] = ()
    day: date | None = None
    date_from: date | None = None
    date_to: date | None = None
    status: StatusFilter | None = None
    competitions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Refuse what the provider would answer with 400."""
        _check_dates(self.date_from, self.date_to)
        if self.day is not None and self.date_from is not None:
            msg = "use either day or dateFrom/dateTo"
            raise ValueError(msg)

    def params(self) -> QueryParams:
        """The query string."""
        params: QueryParams = {}
        _put(params, "ids", self.ids)
        _put(params, "date", self.day)
        _put(params, "dateFrom", self.date_from)
        _put(params, "dateTo", self.date_to)
        _put(params, "status", self.status)
        _put(params, "competitions", self.competitions)
        return params


@dataclass(frozen=True, slots=True, kw_only=True)
class Head2HeadFilters:
    """``GET /matches/{id}/head2head``: earlier meetings of the two teams of a match."""

    limit: int | None = None
    date_from: date | None = None
    date_to: date | None = None
    competitions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Refuse what the provider would answer with 400."""
        _check_dates(self.date_from, self.date_to)
        _check_positive(self.limit, "limit", MAX_LIMIT)

    def params(self) -> QueryParams:
        """The query string."""
        params: QueryParams = {}
        _put(params, "limit", self.limit)
        _put(params, "dateFrom", self.date_from)
        _put(params, "dateTo", self.date_to)
        _put(params, "competitions", self.competitions)
        return params
