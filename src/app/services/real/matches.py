"""Live matches for the bot: Football-Data.org through :class:`~app.pipeline.matches.MatchFeed`.

Works with either backend: it needs only the feed and the user's time zone (from whichever
:class:`~app.services.interfaces.SettingsService` the bot runs with), never the database.
"""

from datetime import date
from typing import Final

from app.logging import get_logger
from app.pipeline.errors import MatchNotFoundError, ProviderUnavailableError
from app.pipeline.matches import Match, MatchFeed, MatchStatus, MatchTeam, Player
from app.services.errors import NotFoundError, ServiceUnavailableError
from app.services.interfaces import (
    LineupPlayer,
    MatchItem,
    MatchSheet,
    MatchState,
    SettingsService,
    SideLineup,
    check_match_days,
)

logger = get_logger(__name__)

COMPONENT: Final = "football-data"
_STATES: Final = {
    MatchStatus.SCHEDULED: MatchState.SCHEDULED,
    MatchStatus.TIMED: MatchState.SCHEDULED,
    MatchStatus.IN_PLAY: MatchState.LIVE,
    MatchStatus.PAUSED: MatchState.LIVE,
    MatchStatus.EXTRA_TIME: MatchState.LIVE,
    MatchStatus.PENALTY_SHOOTOUT: MatchState.LIVE,
    MatchStatus.FINISHED: MatchState.FINISHED,
    MatchStatus.AWARDED: MatchState.FINISHED,
    MatchStatus.POSTPONED: MatchState.POSTPONED,
    MatchStatus.SUSPENDED: MatchState.POSTPONED,
    MatchStatus.CANCELLED: MatchState.CANCELLED,
}


class RealMatchesService:
    """:class:`~app.services.interfaces.MatchesService` over the live provider.

    ``feed`` is ``None`` when ``FOOTBALL_DATA_ORG_KEY`` is not set: every call then says the
    provider is not configured instead of showing anything.
    """

    def __init__(self, feed: MatchFeed | None, settings: SettingsService) -> None:
        """Wire the feed and the source of the user's time zone."""
        self._feed = feed
        self._settings = settings

    async def list_matches(
        self, user_id: int, day_from: date, day_to: date
    ) -> tuple[MatchItem, ...]:
        """See :meth:`app.services.interfaces.MatchesService.list_matches`."""
        check_match_days(day_from, day_to)
        feed = self._require_feed()
        zone = (await self._settings.get_settings(user_id)).zone
        try:
            matches = await feed.matches_on(day_from, day_to, zone)
        except ProviderUnavailableError as error:
            logger.warning("matches_unavailable", user_id=user_id, detail=error.detail)
            raise ServiceUnavailableError(COMPONENT, error.detail) from error
        return tuple(match_item(match) for match in matches)

    async def get_match(self, user_id: int, match_id: int) -> MatchSheet:
        """See :meth:`app.services.interfaces.MatchesService.get_match`."""
        feed = self._require_feed()
        try:
            match = await feed.match(match_id)
        except MatchNotFoundError as error:
            raise NotFoundError("match", match_id) from error
        except ProviderUnavailableError as error:
            logger.warning("match_unavailable", user_id=user_id, match_id=match_id)
            raise ServiceUnavailableError(COMPONENT, error.detail) from error
        return match_sheet(match)

    def _require_feed(self) -> MatchFeed:
        if self._feed is None:
            raise ServiceUnavailableError(COMPONENT, "FOOTBALL_DATA_ORG_KEY is not set")
        return self._feed


def match_item(match: Match) -> MatchItem:
    """The list line of ``match``."""
    competition = match.competition
    return MatchItem(
        match_id=match.id,
        kickoff=match.utc_date,
        competition="" if competition is None else competition.name,
        competition_code=None if competition is None else competition.code,
        home=match.home_team.name,
        away=match.away_team.name,
        state=_STATES.get(match.status, MatchState.OTHER),
        home_goals=match.score.full_time.home,
        away_goals=match.score.full_time.away,
    )


def match_sheet(match: Match) -> MatchSheet:
    """The full sheet of ``match``."""
    return MatchSheet(
        match=match_item(match),
        home_ht_goals=match.score.half_time.home,
        away_ht_goals=match.score.half_time.away,
        home=_side(match.home_team),
        away=_side(match.away_team),
    )


def _side(team: MatchTeam) -> SideLineup:
    return SideLineup(
        team=team.name,
        formation=team.formation,
        coach=None if team.coach is None else team.coach.name,
        starters=_players(team.lineup),
        bench=_players(team.bench),
    )


def _players(players: tuple[Player, ...]) -> tuple[LineupPlayer, ...]:
    """Named players only: a nameless entry carries nothing worth showing."""
    return tuple(
        LineupPlayer(name=player.name, position=player.position, shirt_number=player.shirt_number)
        for player in players
        if player.name
    )
