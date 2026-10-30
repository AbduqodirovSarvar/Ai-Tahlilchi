"""Data matching engine: Football-Data.org matches to football-data.co.uk rows.

A match and a row are the same game when both teams are the same clubs and the dates
agree. Names differ between the sources (``Manchester United FC`` / ``Man United``), so
clubs are paired by :func:`app.sports.team_matching.match_teams` (alias table, exact and
legal-form-free keys, then a strict fuzzy match); only confident pairs are used, so a
wrong price is never attached. Dates are compared in UK time, the file's own clock, with
a tolerance of :data:`DATE_TOLERANCE` for a game moved by a day after the file was saved.

Match one division at a time: names are only compared within a league.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from app.clients.football_data_org import PROVIDER
from app.clients.football_data_org_models import Match, MatchTeam
from app.domain import ExternalRef, Sport, Team
from app.logging import get_logger
from app.sports.aggregator.models import LinkStatus, OddsRow
from app.sports.history.football_data import SOURCE as CSV_SOURCE
from app.sports.team_matching import (
    DEFAULT_MARGIN,
    DEFAULT_THRESHOLD,
    TEAM_ALIASES,
    match_teams,
)

logger = get_logger(__name__)

CSV_TIMEZONE: Final = ZoneInfo("Europe/London")
DATE_TOLERANCE: Final = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class MatchLink:
    """The row a match was linked to (``None`` unless a row was found) and why."""

    status: LinkStatus
    row: OddsRow | None = None


def csv_day(match: Match) -> date:
    """The match date as football-data.co.uk writes it (UK local date of kick-off)."""
    return match.utc_date.astimezone(CSV_TIMEZONE).date()


class MatchingEngine:
    """Links matches of one division to that division's rows (see module docstring)."""

    def __init__(
        self,
        *,
        aliases: Mapping[str, str] = TEAM_ALIASES,
        threshold: float = DEFAULT_THRESHOLD,
        margin: float = DEFAULT_MARGIN,
    ) -> None:
        """``aliases`` maps normalized provider names to normalized CSV names."""
        self._aliases = aliases
        self._threshold = threshold
        self._margin = margin

    def link(self, matches: Sequence[Match], rows: Sequence[OddsRow]) -> dict[int, MatchLink]:
        """A link for every match, keyed by match id."""
        names = self.team_names(matches, rows)
        by_pair: dict[tuple[str, str], list[OddsRow]] = {}
        for row in rows:
            by_pair.setdefault((row.home_team, row.away_team), []).append(row)
        return {match.id: _link(match, names, by_pair) for match in matches}

    def team_names(self, matches: Iterable[Match], rows: Iterable[OddsRow]) -> dict[int, str]:
        """Provider team id -> CSV spelling, for every confidently matched team."""
        provider_teams = {
            team.id: _provider_team(team.id, team.name)
            for match in matches
            for team in (match.home_team, match.away_team)
            if team.id is not None and team.name
        }
        spellings = dict.fromkeys(name for row in rows for name in (row.home_team, row.away_team))
        csv_teams = [_csv_team(name) for name in spellings]
        if not provider_teams or not csv_teams:
            return {}
        report = match_teams(
            provider_teams.values(),
            csv_teams,
            aliases=self._aliases,
            threshold=self._threshold,
            margin=self._margin,
        )
        if report.ambiguous or report.unmatched:
            logger.warning(
                "odds_team_matching_incomplete",
                ambiguous=sorted(item.provider.name for item in report.ambiguous),
                unmatched=sorted(team.name for team in report.unmatched),
            )
        return {int(item.provider.ref.external_id): item.history.name for item in report.matched}


def _link(
    match: Match,
    names: Mapping[int, str],
    by_pair: Mapping[tuple[str, str], Sequence[OddsRow]],
) -> MatchLink:
    home, away = _name(match.home_team, names), _name(match.away_team, names)
    if home is None or away is None:
        return MatchLink(LinkStatus.TEAMS_UNMATCHED)
    day = csv_day(match)
    nearby = [row for row in by_pair.get((home, away), ()) if abs(row.day - day) <= DATE_TOLERANCE]
    if not nearby:
        return MatchLink(LinkStatus.NO_ROW)
    row = min(nearby, key=lambda candidate: abs(candidate.day - day))
    return MatchLink(LinkStatus.MATCHED if row.odds else LinkStatus.NO_ODDS, row)


def _name(team: MatchTeam, names: Mapping[int, str]) -> str | None:
    return None if team.id is None else names.get(team.id)


def _provider_team(team_id: int, name: str) -> Team:
    return Team(ref=ExternalRef(PROVIDER, str(team_id)), sport=Sport.FOOTBALL, name=name)


def _csv_team(name: str) -> Team:
    return Team(ref=ExternalRef(CSV_SOURCE, name), sport=Sport.FOOTBALL, name=name)
