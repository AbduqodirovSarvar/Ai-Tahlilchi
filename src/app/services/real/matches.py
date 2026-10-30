"""Live matches for the bot: Football-Data.org through :class:`~app.pipeline.matches.MatchFeed`.

Works with either backend: the list and the sheets need only the feed and the user's time zone
(from whichever :class:`~app.services.interfaces.SettingsService` the bot runs with). Forecasts
also need the stored history, so they come from a :class:`~app.pipeline.forecast.Forecaster`
over the real pipeline; without one (the sample backend) a forecast says it is unavailable.
"""

from collections.abc import Awaitable
from datetime import date
from typing import Final

from app.logging import get_logger
from app.pipeline.analyst import Analyst
from app.pipeline.card_forecast import CardOutlook as PipelineCardOutlook
from app.pipeline.errors import MatchNotFoundError, ProviderUnavailableError
from app.pipeline.forecast import Forecast, Forecaster
from app.pipeline.matches import Match, MatchFeed, MatchStatus, MatchTeam, Player
from app.services.errors import NotFoundError, ServiceUnavailableError
from app.services.interfaces import (
    CardChance,
    CardOutlook,
    CardScope,
    ConfidenceLevel,
    ExpertReport,
    ForecastLine,
    ForecastStatus,
    LineupPlayer,
    MatchForecast,
    MatchItem,
    MatchSheet,
    MatchState,
    OutcomeChance,
    ScoreProbability,
    SettingsService,
    SideLineup,
    TeamChance,
    TeamPick,
    TopPick,
    check_match_days,
    rank_picks,
)
from app.services.real.expert import expert_report

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

    def __init__(
        self,
        feed: MatchFeed | None,
        settings: SettingsService,
        forecaster: Forecaster | None = None,
        analyst: Analyst | None = None,
    ) -> None:
        """Wire the feed, the user's time zone source, the forecasts and the analyst."""
        self._feed = feed
        self._settings = settings
        self._forecaster = forecaster
        self._analyst = analyst

    async def list_matches(
        self, user_id: int, day_from: date, day_to: date
    ) -> tuple[MatchItem, ...]:
        """See :meth:`app.services.interfaces.MatchesService.list_matches`."""
        check_match_days(day_from, day_to)
        return tuple(match_item(match) for match in await self._day(user_id, day_from, day_to))

    async def get_match(self, user_id: int, match_id: int) -> MatchSheet:
        """See :meth:`app.services.interfaces.MatchesService.get_match`."""
        return match_sheet(await self._match(user_id, match_id))

    async def forecast_day(self, user_id: int, day: date) -> tuple[ForecastLine, ...]:
        """See :meth:`app.services.interfaces.MatchesService.forecast_day`."""
        return await self._lines(user_id, day, day)

    async def top_picks(self, user_id: int, day_from: date, day_to: date) -> tuple[TopPick, ...]:
        """See :meth:`app.services.interfaces.MatchesService.top_picks`."""
        check_match_days(day_from, day_to)
        settings = await self._settings.get_settings(user_id)
        lines = await self._lines(user_id, day_from, day_to)
        return rank_picks(lines, min_probability=settings.min_probability)

    async def expert_picks(self, user_id: int, day_from: date, day_to: date) -> ExpertReport:
        """See :meth:`app.services.interfaces.MatchesService.expert_picks`."""
        check_match_days(day_from, day_to)
        upcoming = [
            match
            for match in await self._day(user_id, day_from, day_to)
            if _STATES.get(match.status) is MatchState.SCHEDULED
        ]
        items = [match_item(match) for match in upcoming]
        if self._analyst is None or not upcoming:
            return ExpertReport(without_forecast=len(items))
        return expert_report(items, await self._analyst.analyse(upcoming))

    async def _lines(self, user_id: int, day_from: date, day_to: date) -> tuple[ForecastLine, ...]:
        matches = await self._day(user_id, day_from, day_to)
        forecasts = await self._forecasts(matches)
        return tuple(
            ForecastLine(match=match_item(match), forecast=forecasts[match.id]) for match in matches
        )

    async def forecast_match(self, user_id: int, match_id: int) -> ForecastLine:
        """See :meth:`app.services.interfaces.MatchesService.forecast_match`."""
        match = await self._match(user_id, match_id)
        forecasts = await self._forecasts([match])
        return ForecastLine(match=match_item(match), forecast=forecasts[match.id])

    async def _day(self, user_id: int, day_from: date, day_to: date) -> list[Match]:
        feed = self._require_feed()
        zone = (await self._settings.get_settings(user_id)).zone
        return await _provider(feed.matches_on(day_from, day_to, zone), user_id=user_id)

    async def _match(self, user_id: int, match_id: int) -> Match:
        feed = self._require_feed()
        try:
            return await _provider(feed.match(match_id), user_id=user_id)
        except MatchNotFoundError as error:
            raise NotFoundError("match", match_id) from error

    async def _forecasts(self, matches: list[Match]) -> dict[int, MatchForecast]:
        if self._forecaster is None:
            return {
                m.id: MatchForecast(match_id=m.id, status=ForecastStatus.UNAVAILABLE)
                for m in matches
            }
        forecasts = await self._forecaster.forecast(matches)
        return {match_id: match_forecast(f) for match_id, f in forecasts.items()}

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


async def _provider[T](call: Awaitable[T], *, user_id: int) -> T:
    """Await a feed call; a provider outage becomes the contract's "try again later"."""
    try:
        return await call
    except ProviderUnavailableError as error:
        logger.warning("matches_unavailable", user_id=user_id, detail=error.detail)
        raise ServiceUnavailableError(COMPONENT, error.detail) from error


def match_forecast(forecast: Forecast) -> MatchForecast:
    """The contract view of a pipeline forecast."""
    if forecast.skip is not None:
        return MatchForecast(
            match_id=forecast.match_id,
            status=ForecastStatus(forecast.skip.value),
            team_matches=forecast.team_matches,
        )
    return MatchForecast(
        match_id=forecast.match_id,
        status=ForecastStatus.READY,
        chances=tuple(
            OutcomeChance(
                market=c.market,
                selection=c.selection,
                probability=_unit(c.probability),
                push=_unit(c.push),
            )
            for c in forecast.chances
        ),
        scores=tuple(
            ScoreProbability(
                home_goals=s.home_goals, away_goals=s.away_goals, probability=_unit(s.probability)
            )
            for s in forecast.scores
        ),
        team_chances=tuple(
            TeamChance(pick=TeamPick(c.pick.value), probability=_unit(c.probability))
            for c in forecast.team_chances
        ),
        cards=_cards(forecast.cards),
        team_matches=forecast.team_matches,
        confidence=None
        if forecast.confidence is None
        else ConfidenceLevel(forecast.confidence.value),
    )


def _unit(probability: float) -> float:
    """``probability`` kept inside [0, 1] (float sums may overshoot by an ulp)."""
    return min(max(probability, 0.0), 1.0)


def _cards(outlook: PipelineCardOutlook | None) -> CardOutlook | None:
    if outlook is None:
        return None
    return CardOutlook(
        expected_home=outlook.expected_home,
        expected_away=outlook.expected_away,
        referee=outlook.referee,
        referee_known=outlook.referee_known,
        strictness=outlook.strictness,
        team_matches=outlook.team_matches,
        chances=tuple(
            CardChance(
                scope=CardScope(c.scope.value),
                line=c.line,
                over=c.over,
                probability=_unit(c.probability),
            )
            for c in outlook.chances
        ),
    )
