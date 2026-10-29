"""Tracked football leagues and their ids at each data source.

The live provider is Football-Data.org (competition codes such as ``PL``, ADR 0025); the
history import is football-data.co.uk (division codes such as ``E0``). The API-Football ids of
the ten default leagues (decision log #5) stay only so rows stored before API-Football was
removed still resolve to the same competition.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Final

from app.domain import ExternalRef, League, Sport
from app.sports.history.football_data import LEAGUES as FOOTBALL_DATA_LEAGUES
from app.sports.history.football_data import SOURCE as FOOTBALL_DATA_SOURCE

API_FOOTBALL_SOURCE: Final = "api-football"
"""Legacy source of rows stored before ADR 0025 (no requests go there any more)."""
FOOTBALL_DATA_ORG_SOURCE: Final = "football-data-org"
"""Source of every fixture, team and league fetched from api.football-data.org."""
# European seasons start in July/August; a date before July belongs to the season that
# started the previous calendar year (season 2024 = 2024/25).
SEASON_FIRST_MONTH: Final = 7


@dataclass(frozen=True, slots=True)
class TrackedLeague:
    """One league and its identity at each source.

    ``provider_code`` is the Football-Data.org competition code, ``football_data_code`` the
    football-data.co.uk division and ``api_id`` the legacy API-Football id (stored rows only).
    """

    api_id: int
    football_data_code: str
    provider_code: str

    @property
    def api_ref(self) -> ExternalRef:
        """Legacy identity at API-Football (rows stored before ADR 0025)."""
        return ExternalRef(API_FOOTBALL_SOURCE, str(self.api_id))

    @property
    def provider_ref(self) -> ExternalRef:
        """Identity at Football-Data.org, the live provider."""
        return ExternalRef(FOOTBALL_DATA_ORG_SOURCE, self.provider_code)

    @property
    def football_data_ref(self) -> ExternalRef:
        """Identity at football-data.co.uk (history import)."""
        return ExternalRef(FOOTBALL_DATA_SOURCE, self.football_data_code)

    def football_data_league(self) -> League:
        """The league exactly as the history importer writes it (stable upserts)."""
        info = FOOTBALL_DATA_LEAGUES[self.football_data_code]
        return League(
            ref=self.football_data_ref, sport=Sport.FOOTBALL, name=info.name, country=info.country
        )


DEFAULT_LEAGUES: Final = (
    TrackedLeague(39, "E0", "PL"),  # England, Premier League
    TrackedLeague(40, "E1", "ELC"),  # England, Championship
    TrackedLeague(140, "SP1", "PD"),  # Spain, La Liga
    TrackedLeague(78, "D1", "BL1"),  # Germany, Bundesliga
    TrackedLeague(135, "I1", "SA"),  # Italy, Serie A
    TrackedLeague(61, "F1", "FL1"),  # France, Ligue 1
    TrackedLeague(88, "N1", "DED"),  # Netherlands, Eredivisie
    TrackedLeague(94, "P1", "PPL"),  # Portugal, Primeira Liga
    # Not on Football-Data's free plan (TIER_ONE): served only with a paid plan.
    TrackedLeague(144, "B1", "BJL"),  # Belgium, Jupiler Pro League
    TrackedLeague(203, "T1", "TSL"),  # Turkey, Süper Lig
)


class LeagueCatalog:
    """Lookup of tracked leagues by provider code or by any source's reference."""

    def __init__(self, leagues: Iterable[TrackedLeague] = DEFAULT_LEAGUES) -> None:
        """Index ``leagues``; duplicate ids or codes are a configuration error."""
        self._leagues = tuple(leagues)
        self._by_api_id: Mapping[int, TrackedLeague] = {lg.api_id: lg for lg in self._leagues}
        self._by_code: Mapping[str, TrackedLeague] = {lg.provider_code: lg for lg in self._leagues}
        self._by_ref: Mapping[ExternalRef, TrackedLeague] = {
            ref: league
            for league in self._leagues
            for ref in (league.api_ref, league.football_data_ref, league.provider_ref)
        }
        if len(self._by_ref) != 3 * len(self._leagues):
            msg = "tracked leagues must have unique ids and codes at every source"
            raise ValueError(msg)

    @property
    def leagues(self) -> tuple[TrackedLeague, ...]:
        """Every tracked league, in configuration order."""
        return self._leagues

    def by_api_id(self, api_id: int) -> TrackedLeague | None:
        """The tracked league with this legacy API-Football id, if tracked."""
        return self._by_api_id.get(api_id)

    def by_provider_code(self, code: str | None) -> TrackedLeague | None:
        """The tracked league with this Football-Data.org competition code, if tracked."""
        return None if code is None else self._by_code.get(code)

    def resolve(self, ref: ExternalRef) -> TrackedLeague | None:
        """The tracked league for a reference of either source, if tracked."""
        return self._by_ref.get(ref)


def season_for(day: date) -> int:
    """Start year of the season that ``day`` belongs to (2025-03-01 → 2024)."""
    return day.year if day.month >= SEASON_FIRST_MONTH else day.year - 1


def seasons_between(date_from: date, date_to: date) -> tuple[int, ...]:
    """Every season a date range touches (a window across 1 July spans two)."""
    return tuple(range(season_for(date_from), season_for(date_to) + 1))
