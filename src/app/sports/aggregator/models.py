"""Result types of the data aggregator: one match from both sources, and how it was linked."""

import math
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from typing import Final

from app.clients.football_data_org_models import TeamLineup
from app.errors import DataError

BET365: Final = "Bet365"


@dataclass(frozen=True, slots=True)
class MatchOdds:
    """1X2 decimal prices of one bookmaker (football-data.co.uk ``B365H/B365D/B365A``)."""

    home_win: float
    draw: float
    away_win: float
    bookmaker: str = BET365

    def __post_init__(self) -> None:
        """A decimal price is finite and above 1, or it is not a price."""
        for price in (self.home_win, self.draw, self.away_win):
            if not math.isfinite(price) or price <= 1.0:
                msg = f"decimal odds must be finite and above 1, got {price!r}"
                raise DataError(msg)


@dataclass(frozen=True, slots=True)
class OddsRow:
    """One football-data.co.uk row: a match by date and team spelling, with its prices.

    ``day`` and ``kickoff`` are UK local time, as in the file; ``odds`` is ``None`` when the
    Bet365 columns are missing or broken (the row still identifies the match).
    """

    division: str
    day: date
    home_team: str
    away_team: str
    kickoff: time | None = None
    odds: MatchOdds | None = None


class LinkStatus(StrEnum):
    """Why a match has, or has no, odds from the CSV."""

    MATCHED = "matched"
    NO_ODDS = "no_odds"  # the CSV row exists but its Bet365 prices are empty
    NO_ROW = "no_row"  # teams known, but no row on the match date (not published yet)
    TEAMS_UNMATCHED = "teams_unmatched"  # a team name has no confident counterpart
    LEAGUE_NOT_COVERED = "league_not_covered"  # no football-data.co.uk division for it
    SOURCE_UNAVAILABLE = "source_unavailable"  # the CSV could not be downloaded or read


@dataclass(frozen=True, slots=True)
class CombinedMatchDto:
    """A Football-Data.org match joined with its football-data.co.uk Bet365 prices.

    ``match_date`` is the UTC kick-off. Line-ups are ``None`` until announced (or when the
    plan does not include them); ``odds`` is ``None`` unless ``link`` is ``MATCHED``.
    """

    match_id: int
    match_date: datetime
    league: str
    league_code: str | None
    home_team: str
    away_team: str
    home_lineup: TeamLineup | None
    away_lineup: TeamLineup | None
    odds: MatchOdds | None
    link: LinkStatus
