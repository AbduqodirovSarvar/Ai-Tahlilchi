"""A self-contained bundle of provider data, handed from adapters/importers to storage."""

from dataclasses import dataclass

from app.domain.entities import ExternalRef, Fixture, FixtureResult, League, Team
from app.domain.odds import OddsSnapshot


@dataclass(frozen=True, slots=True, kw_only=True)
class HistoryBatch:
    """Fixtures with their results and odds, as produced by one fetch or one file.

    Leagues and teams are not listed separately: every one that matters is reachable from
    ``fixtures``. Results and odds refer to fixtures by :class:`~app.domain.ExternalRef`.
    """

    fixtures: tuple[Fixture, ...] = ()
    results: tuple[FixtureResult, ...] = ()
    odds: tuple[OddsSnapshot, ...] = ()

    @property
    def leagues(self) -> tuple[League, ...]:
        """Distinct leagues of ``fixtures``, in first-seen order."""
        unique = {fixture.league.ref: fixture.league for fixture in self.fixtures}
        return tuple(unique.values())

    @property
    def teams(self) -> tuple[Team, ...]:
        """Distinct teams of ``fixtures``, in first-seen order."""
        unique: dict[ExternalRef, Team] = {}
        for fixture in self.fixtures:
            unique.setdefault(fixture.home.ref, fixture.home)
            unique.setdefault(fixture.away.ref, fixture.away)
        return tuple(unique.values())
