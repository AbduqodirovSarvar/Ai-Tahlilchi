"""Tracked football leagues and their ids at each data source.

API-Football league ids of the ten default leagues (decision log #5). They are stable
across seasons; the probe script (F0.7) prints the provider's own name for each id so a
wrong id shows up immediately once a key exists.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Final

from app.domain import ExternalRef, League, Sport
from app.sports.history.football_data import LEAGUES as FOOTBALL_DATA_LEAGUES
from app.sports.history.football_data import SOURCE as FOOTBALL_DATA_SOURCE

API_FOOTBALL_SOURCE: Final = "api-football"
# European seasons start in July/August; a date before July belongs to the season that
# started the previous calendar year (season 2024 = 2024/25).
SEASON_FIRST_MONTH: Final = 7


@dataclass(frozen=True, slots=True)
class TrackedLeague:
    """One league with its id at API-Football and its football-data.co.uk division code."""

    api_id: int
    football_data_code: str

    @property
    def api_ref(self) -> ExternalRef:
        """Identity at API-Football."""
        return ExternalRef(API_FOOTBALL_SOURCE, str(self.api_id))

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
    TrackedLeague(39, "E0"),  # England, Premier League
    TrackedLeague(40, "E1"),  # England, Championship
    TrackedLeague(140, "SP1"),  # Spain, La Liga
    TrackedLeague(78, "D1"),  # Germany, Bundesliga
    TrackedLeague(135, "I1"),  # Italy, Serie A
    TrackedLeague(61, "F1"),  # France, Ligue 1
    TrackedLeague(88, "N1"),  # Netherlands, Eredivisie
    TrackedLeague(94, "P1"),  # Portugal, Primeira Liga
    TrackedLeague(144, "B1"),  # Belgium, Jupiler Pro League
    TrackedLeague(203, "T1"),  # Turkey, Süper Lig
)


class LeagueCatalog:
    """Lookup of tracked leagues by API-Football id or by either source's reference."""

    def __init__(self, leagues: Iterable[TrackedLeague] = DEFAULT_LEAGUES) -> None:
        """Index ``leagues``; duplicate ids or codes are a configuration error."""
        self._leagues = tuple(leagues)
        self._by_api_id: Mapping[int, TrackedLeague] = {lg.api_id: lg for lg in self._leagues}
        self._by_ref: Mapping[ExternalRef, TrackedLeague] = {
            ref: league
            for league in self._leagues
            for ref in (league.api_ref, league.football_data_ref)
        }
        if len(self._by_ref) != 2 * len(self._leagues):
            msg = "tracked leagues must have unique API-Football ids and football-data codes"
            raise ValueError(msg)

    @property
    def leagues(self) -> tuple[TrackedLeague, ...]:
        """Every tracked league, in configuration order."""
        return self._leagues

    def by_api_id(self, api_id: int) -> TrackedLeague | None:
        """The tracked league with this API-Football id, if tracked."""
        return self._by_api_id.get(api_id)

    def resolve(self, ref: ExternalRef) -> TrackedLeague | None:
        """The tracked league for a reference of either source, if tracked."""
        return self._by_ref.get(ref)


def season_for(day: date) -> int:
    """Start year of the season that ``day`` belongs to (2025-03-01 → 2024)."""
    return day.year if day.month >= SEASON_FIRST_MONTH else day.year - 1


def seasons_between(date_from: date, date_to: date) -> tuple[int, ...]:
    """Every season a date range touches (a window across 1 July spans two)."""
    return tuple(range(season_for(date_from), season_for(date_to) + 1))
