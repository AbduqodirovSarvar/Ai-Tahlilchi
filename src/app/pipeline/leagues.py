"""Which stored leagues are the same competition, and one identity for provider fixtures.

A league may be stored under two references (football-data history ``E0`` and API-Football
``39``) until team matching unifies them; model fitting must read both. Fixtures fetched
from the provider are rewritten to the history identity (teams and league) before they are
stored, so history and live data of a club share one row (``IdentityMap``, ADR 0007 §8).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from app.domain import ExternalRef, Fixture, Team
from app.logging import get_logger
from app.sports.football_leagues import LeagueCatalog
from app.sports.team_matching import IdentityMap, match_teams

logger = get_logger(__name__)

HISTORY_SOURCE: Final = "football-data"
"""Source whose team spellings are the canonical identity (history import)."""


@dataclass(frozen=True, slots=True)
class MatchingSummary:
    """How many provider teams of a league matched a history team (logged, not hidden)."""

    league: ExternalRef
    matched: int
    fuzzy: int
    ambiguous: int
    unmatched: int


class LeagueDirectory:
    """Lookup of tracked leagues by any of their references."""

    def __init__(self, catalog: LeagueCatalog | None = None) -> None:
        """Wrap ``catalog`` (default: the ten tracked football leagues)."""
        self._catalog = catalog or LeagueCatalog()

    def group(self, ref: ExternalRef) -> tuple[ExternalRef, ...]:
        """Every reference under which ``ref``'s competition may be stored."""
        tracked = self._catalog.resolve(ref)
        if tracked is None:
            return (ref,)
        return (tracked.football_data_ref, tracked.api_ref)

    def canonical(self, ref: ExternalRef) -> ExternalRef:
        """The history reference of the competition (``ref`` itself when untracked)."""
        tracked = self._catalog.resolve(ref)
        return ref if tracked is None else tracked.football_data_ref

    def tracked(self, ref: ExternalRef) -> bool:
        """True when ``ref`` is one of the tracked competitions."""
        return self._catalog.resolve(ref) is not None

    def canonical_refs(self) -> tuple[ExternalRef, ...]:
        """History references of every tracked competition."""
        return tuple(league.football_data_ref for league in self._catalog.leagues)

    def canonicalize(
        self, fixtures: Sequence[Fixture], history: Mapping[ExternalRef, Sequence[Team]]
    ) -> tuple[list[Fixture], list[MatchingSummary]]:
        """``fixtures`` with teams and leagues rewritten to their history identity.

        ``history`` maps a canonical league reference to the history teams stored for it.
        Only alias/exact matches are applied (fuzzy ones are reported, not linked); unmatched
        teams keep their provider identity and are counted in the returned summaries so the
        caller can log them.
        """
        by_league: dict[ExternalRef, list[Fixture]] = {}
        for fixture in fixtures:
            by_league.setdefault(self.canonical(fixture.league.ref), []).append(fixture)
        teams: dict[ExternalRef, Team] = {}
        summaries = []
        for league, items in by_league.items():
            provider_teams = [team for item in items for team in (item.home, item.away)]
            report = match_teams(provider_teams, list(history.get(league, ())))
            # Name-similarity-only matches need a human look: never linked silently.
            teams.update(
                {m.provider.ref: m.history for m in report.matched if m not in report.fuzzy}
            )
            summaries.append(
                MatchingSummary(
                    league=league,
                    matched=len(report.matched) - len(report.fuzzy),
                    fuzzy=len(report.fuzzy),
                    ambiguous=len(report.ambiguous),
                    unmatched=len(report.unmatched),
                )
            )
        merged = IdentityMap.from_report(match_teams([], []), self._catalog)
        identity = IdentityMap(teams=teams, leagues=merged.leagues)
        return [identity.apply(fixture) for fixture in fixtures], summaries
