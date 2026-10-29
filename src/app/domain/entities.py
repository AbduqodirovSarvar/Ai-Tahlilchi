"""Leagues, teams, fixtures and their results.

Entities are identified across the system by an :class:`ExternalRef` (``source`` +
``external_id``) rather than by database ids, so data providers can be swapped or combined
without the domain depending on storage.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from app.domain._validation import (
    MAX_IDENTIFIER_LENGTH,
    require_finite,
    require_non_negative_int,
    require_slug,
    require_text,
    require_utc,
)
from app.domain.errors import DomainValidationError
from app.domain.sport import Sport

MIN_SEASON: Final = 1850
MAX_SEASON: Final = 2200

_APOSTROPHES: Final = re.compile("['`\u2019\u00b4]")
_NON_ALPHANUMERIC: Final = re.compile(r"[^0-9a-z]+")


def normalize_name(name: str) -> str:
    """Return a lookup key for a team/league name: no accents, case or punctuation.

    ``"Nott'm Forest"`` → ``"nottm forest"``, ``"Bayern München"`` → ``"bayern munchen"``.
    Used to match the same club across sources; the original spelling is always kept too.
    """
    decomposed = unicodedata.normalize("NFKD", name)
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    folded = _APOSTROPHES.sub("", without_marks.casefold())
    return _NON_ALPHANUMERIC.sub(" ", folded).strip()


@dataclass(frozen=True, slots=True)
class ExternalRef:
    """Identity of an entity at a data source, e.g. ``("api-football", "39")``."""

    source: str
    external_id: str

    def __post_init__(self) -> None:
        """Validate the source slug and the id."""
        require_slug(self.source, "source")
        object.__setattr__(
            self,
            "external_id",
            require_text(self.external_id, "external_id", max_length=MAX_IDENTIFIER_LENGTH),
        )

    def __str__(self) -> str:
        """Readable form for logs: ``source:external_id``."""
        return f"{self.source}:{self.external_id}"


@dataclass(frozen=True, slots=True, kw_only=True)
class League:
    """A competition (league or cup) of one sport."""

    ref: ExternalRef
    sport: Sport
    name: str
    country: str | None = None

    def __post_init__(self) -> None:
        """Strip names; empty names are rejected."""
        object.__setattr__(self, "name", require_text(self.name, "league name"))
        if self.country is not None:
            object.__setattr__(self, "country", require_text(self.country, "league country"))


@dataclass(frozen=True, slots=True, kw_only=True)
class Team:
    """A team (or, for tennis, a player) as named by its source."""

    ref: ExternalRef
    sport: Sport
    name: str
    country: str | None = None

    def __post_init__(self) -> None:
        """Strip names; empty names are rejected."""
        object.__setattr__(self, "name", require_text(self.name, "team name"))
        if self.country is not None:
            object.__setattr__(self, "country", require_text(self.country, "team country"))

    @property
    def normalized_name(self) -> str:
        """Lookup key for cross-source matching (see :func:`normalize_name`)."""
        return normalize_name(self.name)


class FixtureStatus(StrEnum):
    """Lifecycle state of a fixture."""

    SCHEDULED = "scheduled"
    LIVE = "live"
    FINISHED = "finished"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


@dataclass(frozen=True, slots=True, kw_only=True)
class Fixture:
    """A scheduled or played match between two teams of one league and season.

    ``season`` is the calendar year the season starts in (2024 for 2024/25), which is
    also how API-Football numbers seasons. ``kickoff`` is always stored in UTC.
    """

    ref: ExternalRef
    sport: Sport
    league: League
    season: int
    kickoff: datetime
    home: Team
    away: Team
    status: FixtureStatus = FixtureStatus.SCHEDULED

    def __post_init__(self) -> None:
        """Check kickoff, season, distinct teams and that every part is the same sport."""
        object.__setattr__(self, "kickoff", require_utc(self.kickoff, "kickoff"))
        if not MIN_SEASON <= self.season <= MAX_SEASON:
            msg = f"season must be a start year in {MIN_SEASON}..{MAX_SEASON} (got {self.season})"
            raise DomainValidationError(msg)
        if self.home.ref == self.away.ref:
            msg = f"a team cannot play itself ({self.home.ref})"
            raise DomainValidationError(msg)
        sports = {self.league.sport, self.home.sport, self.away.sport}
        if sports != {self.sport}:
            msg = f"fixture {self.ref} mixes sports: {sorted(sports | {self.sport})}"
            raise DomainValidationError(msg)


class ResultStatus(StrEnum):
    """Final state of a fixture once it is no longer expected to be played as scheduled."""

    FINISHED = "finished"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


_SCORE_FIELDS: Final = ("home_score", "away_score", "home_ht_score", "away_ht_score")
CARD_FIELDS: Final = ("home_yellow", "away_yellow", "home_red", "away_red")
"""Card counts of a result (each a non-negative int or ``None``)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class FixtureResult:
    """Outcome of a fixture (full-time scores exclude extra time and penalties).

    A finished fixture has both scores; a postponed or cancelled one has none; an abandoned
    one may carry the score at the time it was stopped. Cards (yellow, red) and the referee
    are optional match facts: sources publish them unevenly (``None`` = not known).
    """

    fixture: ExternalRef
    status: ResultStatus
    home_score: int | None = None
    away_score: int | None = None
    home_ht_score: int | None = None
    away_ht_score: int | None = None
    home_xg: float | None = None
    away_xg: float | None = None
    home_yellow: int | None = None
    away_yellow: int | None = None
    home_red: int | None = None
    away_red: int | None = None
    referee: str | None = None

    def __post_init__(self) -> None:
        """Check scores are consistent with the status and with each other."""
        self._check_scores_for_status()
        for field in (*_SCORE_FIELDS, *CARD_FIELDS):
            require_non_negative_int(getattr(self, field), field)
        referee = None if self.referee is None else " ".join(self.referee.split())
        object.__setattr__(self, "referee", referee or None)
        self._check_half_time("home")
        self._check_half_time("away")
        for field in ("home_xg", "away_xg"):
            self._check_xg(field)

    def _check_scores_for_status(self) -> None:
        has_home, has_away = self.home_score is not None, self.away_score is not None
        if has_home != has_away:
            msg = f"result for {self.fixture} must have both full-time scores or neither"
            raise DomainValidationError(msg)
        if self.status is ResultStatus.FINISHED and not has_home:
            msg = f"finished result for {self.fixture} needs both scores"
            raise DomainValidationError(msg)
        if self.status in {ResultStatus.POSTPONED, ResultStatus.CANCELLED} and has_home:
            msg = f"{self.status} result for {self.fixture} cannot have a score"
            raise DomainValidationError(msg)

    def _check_half_time(self, side: str) -> None:
        full_time: int | None = getattr(self, f"{side}_score")
        half_time: int | None = getattr(self, f"{side}_ht_score")
        if half_time is not None and (full_time is None or half_time > full_time):
            msg = f"{side} half-time score {half_time} exceeds full-time score {full_time}"
            raise DomainValidationError(msg)

    def _check_xg(self, field: str) -> None:
        value: float | None = getattr(self, field)
        if value is None:
            return
        finite = require_finite(value, field)
        if finite < 0:
            msg = f"{field} must be >= 0 (got {value})"
            raise DomainValidationError(msg)
        object.__setattr__(self, field, finite)
