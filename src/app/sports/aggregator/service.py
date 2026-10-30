"""Data aggregator service: Football-Data.org matches + football-data.co.uk Bet365 prices.

Request budget (free plan: 10 requests a minute, paced and cached by
:class:`~app.clients.http.ApiClient`): one ``/matches`` request per 10 days asked, plus at
most ``max_lineup_requests`` match details, only for matches close to kick-off (elevens are
published about an hour before). Line-ups already in the list response cost nothing.

Failure policy: the match list is the backbone, so its errors are raised. Odds and
line-ups are extras: a failed CSV or match detail is logged and the match is returned
without them, with :class:`~app.sports.aggregator.models.LinkStatus` saying why.
"""

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Final

from app.clients.football_data_org import MAX_DAYS_PER_MATCHES_REQUEST, FootballDataClient
from app.clients.football_data_org_models import (
    Match,
    MatchStatus,
    TeamLineup,
    team_lineup,
)
from app.clients.quota import RequestPurpose
from app.errors import DataError, ExternalServiceError
from app.logging import get_logger
from app.sports.aggregator.csv_odds import OddsSource, ParsedOdds
from app.sports.aggregator.matching import MatchingEngine, MatchLink, csv_day
from app.sports.aggregator.models import CombinedMatchDto, LinkStatus, OddsRow
from app.sports.football_leagues import LeagueCatalog, season_for

logger = get_logger(__name__)

LINEUP_LOOKAHEAD: Final = timedelta(hours=2)
# A match still being played keeps its line-ups worth fetching for this long.
LINEUP_LOOKBACK: Final = timedelta(hours=3)
MAX_LINEUP_REQUESTS: Final = 5
_FINAL_STATUSES: Final = frozenset(
    {
        MatchStatus.FINISHED,
        MatchStatus.AWARDED,
        MatchStatus.POSTPONED,
        MatchStatus.CANCELLED,
        MatchStatus.SUSPENDED,
    }
)

type Lineups = tuple[TeamLineup | None, TeamLineup | None]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DataAggregatorService:
    """Builds :class:`CombinedMatchDto` lists (see module docstring)."""

    def __init__(  # noqa: PLR0913 - every collaborator is injected for tests
        self,
        client: FootballDataClient,
        odds: OddsSource,
        *,
        catalog: LeagueCatalog | None = None,
        engine: MatchingEngine | None = None,
        clock: Callable[[], datetime] = _utc_now,
        max_lineup_requests: int = MAX_LINEUP_REQUESTS,
    ) -> None:
        """``catalog`` maps competitions to football-data.co.uk divisions."""
        self._client = client
        self._odds = odds
        self._catalog = catalog or LeagueCatalog()
        self._engine = engine or MatchingEngine()
        self._clock = clock
        self._max_lineup_requests = max_lineup_requests

    async def combined_matches(
        self,
        day_from: date,
        day_to: date,
        *,
        purpose: RequestPurpose = RequestPurpose.MANUAL,
    ) -> tuple[CombinedMatchDto, ...]:
        """Every match kicking off on the UTC days ``day_from``..``day_to``, by kick-off.

        Raises:
            ValueError: the range is reversed or longer than 10 days.
            ExternalServiceError: Football-Data.org did not answer the match list.
        """
        check_range(day_from, day_to)
        matches = await self._client.matches_between(day_from, day_to, purpose=purpose)
        links = await self._links(matches)
        lineups = await self._lineups(matches, purpose)
        ordered = sorted(matches, key=lambda match: (match.utc_date, match.id))
        return tuple(combine(match, links[match.id], lineups[match.id]) for match in ordered)

    def division_of(self, match: Match) -> str | None:
        """football-data.co.uk division of the match's competition, if tracked."""
        code = match.competition.code if match.competition else None
        league = self._catalog.by_provider_code(code)
        return None if league is None else league.football_data_code

    async def _links(self, matches: Sequence[Match]) -> dict[int, MatchLink]:
        links = {match.id: MatchLink(LinkStatus.LEAGUE_NOT_COVERED) for match in matches}
        by_division: dict[str, list[Match]] = {}
        for match in matches:
            division = self.division_of(match)
            if division is not None:
                by_division.setdefault(division, []).append(match)
        if not by_division:
            return links
        # fixtures.csv covers every division: one load per call.
        upcoming = await _safely(self._odds.upcoming(), division=None)
        for division, group in by_division.items():
            rows = await self._rows(division, group, upcoming)
            if rows is None:
                links.update(
                    (match.id, MatchLink(LinkStatus.SOURCE_UNAVAILABLE)) for match in group
                )
            else:
                links.update(self._engine.link(group, rows))
        return links

    async def _rows(
        self, division: str, matches: Sequence[Match], upcoming: ParsedOdds | None
    ) -> list[OddsRow] | None:
        """Season rows (full team list, played games) and coming rows of one division.

        ``None`` when no file could be read at all.
        """
        rows: list[OddsRow] = [] if upcoming is None else list(upcoming.of_division(division))
        loaded = upcoming is not None
        for season in sorted({season_for(csv_day(match)) for match in matches}):
            parsed = await _safely(self._odds.season(division, season), division=division)
            if parsed is not None:
                rows.extend(parsed.rows)
                loaded = True
        return rows if loaded else None

    async def _lineups(
        self, matches: Sequence[Match], purpose: RequestPurpose
    ) -> dict[int, Lineups]:
        found = {match.id: listed_lineups(match) for match in matches}
        now = self._clock()
        wanted = [
            match
            for match in matches
            if found[match.id] == (None, None) and lineups_due(match, now)
        ]
        for match in wanted[: self._max_lineup_requests]:
            found[match.id] = await self._fetch_lineups(match.id, purpose)
        if len(wanted) > self._max_lineup_requests:
            logger.info(
                "lineup_budget_exhausted",
                wanted=len(wanted),
                fetched=self._max_lineup_requests,
            )
        return found

    async def _fetch_lineups(self, match_id: int, purpose: RequestPurpose) -> Lineups:
        try:
            lineups = await self._client.lineups(match_id, purpose=purpose)
        except ExternalServiceError:
            logger.warning("lineups_unavailable", fixture_id=match_id, exc_info=True)
            return None, None
        return _announced(lineups.home), _announced(lineups.away)


def check_range(day_from: date, day_to: date) -> None:
    """Reject a reversed range or one Football-Data.org would need over 1 request for.

    Raises:
        ValueError: ``day_to`` before ``day_from`` or more than 10 days in total.
    """
    if day_to < day_from:
        msg = f"day_to {day_to} is before day_from {day_from}"
        raise ValueError(msg)
    if (day_to - day_from).days + 1 > MAX_DAYS_PER_MATCHES_REQUEST:
        msg = f"at most {MAX_DAYS_PER_MATCHES_REQUEST} days per call"
        raise ValueError(msg)


def lineups_due(match: Match, now: datetime) -> bool:
    """Whether line-ups may be out: kick-off is near or under way, and the match is on."""
    if match.status in _FINAL_STATUSES:
        return False
    return now - LINEUP_LOOKBACK <= match.utc_date <= now + LINEUP_LOOKAHEAD


def listed_lineups(match: Match) -> Lineups:
    """Line-ups the list response already carries (none on most plans)."""
    return _announced(team_lineup(match.home_team)), _announced(team_lineup(match.away_team))


def combine(match: Match, link: MatchLink, lineups: Lineups) -> CombinedMatchDto:
    """One result record from the match, its odds link and its line-ups."""
    competition = match.competition
    return CombinedMatchDto(
        match_id=match.id,
        match_date=match.utc_date.astimezone(UTC),
        league=competition.name if competition and competition.name else "",
        league_code=competition.code if competition else None,
        home_team=match.home_team.name or "",
        away_team=match.away_team.name or "",
        home_lineup=lineups[0],
        away_lineup=lineups[1],
        odds=link.row.odds if link.status is LinkStatus.MATCHED and link.row else None,
        link=link.status,
    )


def _announced(lineup: TeamLineup) -> TeamLineup | None:
    return lineup if lineup.announced else None


async def _safely[T](load: Awaitable[T], *, division: str | None) -> T | None:
    """A CSV load whose failure is logged instead of failing the whole aggregation."""
    try:
        return await load
    except (ExternalServiceError, DataError):
        logger.warning("odds_csv_unavailable", division=division, exc_info=True)
        return None
