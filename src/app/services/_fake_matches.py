"""In-memory :class:`~app.services.interfaces.MatchesService` for bot development and tests.

Two sample matches on every local day (``19:00`` and ``21:30`` in the user's zone), ids derived
from the date so the same day always yields the same ids. Production wiring replaces this
with the live Football-Data.org service whenever ``FOOTBALL_DATA_ORG_KEY`` is set, in both
backends (see ``app.main``).
"""

from datetime import date, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from app.domain import Market, Selection
from app.services.errors import NotFoundError
from app.services.interfaces import (
    ConfidenceLevel,
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
    check_match_days,
)

_SAMPLE_TIMES: Final = (time(19, 0), time(21, 30))
_SAMPLE_TEAMS: Final = (("Arsenal FC", "Chelsea FC"), ("Real Madrid CF", "FC Barcelona"))
_IDS_PER_DAY: Final = 10
_ZONE: Final = ZoneInfo("UTC")
_SAMPLE_RESULT: Final = ((Selection.HOME, 0.52), (Selection.DRAW, 0.26), (Selection.AWAY, 0.22))
_SAMPLE_TOTALS: Final = ((Selection.OVER, 0.55), (Selection.UNDER, 0.45))
_SAMPLE_BTTS: Final = ((Selection.YES, 0.51), (Selection.NO, 0.49))
_SAMPLE_TEAM_MATCHES: Final = 30


def _match_id(day: date, slot: int) -> int:
    return day.toordinal() * _IDS_PER_DAY + slot


def _sample(day: date, slot: int, zone: ZoneInfo) -> MatchItem:
    home, away = _SAMPLE_TEAMS[slot]
    kickoff = datetime.combine(day, _SAMPLE_TIMES[slot], zone).astimezone(_ZONE)
    return MatchItem(
        match_id=_match_id(day, slot),
        kickoff=kickoff,
        competition="Premier League" if slot == 0 else "Primera Division",
        competition_code="PL" if slot == 0 else "PD",
        home=home,
        away=away,
        state=MatchState.SCHEDULED,
    )


class FakeMatchesService:
    """Sample matches; no network."""

    def __init__(self, settings: SettingsService) -> None:
        """``settings`` gives the user's zone, like the real service."""
        self._settings = settings

    async def list_matches(
        self, user_id: int, day_from: date, day_to: date
    ) -> tuple[MatchItem, ...]:
        """See :meth:`app.services.interfaces.MatchesService.list_matches`."""
        check_match_days(day_from, day_to)
        zone = (await self._settings.get_settings(user_id)).zone
        days = (day_from + timedelta(days=offset) for offset in range((day_to - day_from).days + 1))
        return tuple(_sample(day, slot, zone) for day in days for slot in range(len(_SAMPLE_TIMES)))

    async def get_match(self, user_id: int, match_id: int) -> MatchSheet:
        """See :meth:`app.services.interfaces.MatchesService.get_match`."""
        day, slot = divmod(match_id, _IDS_PER_DAY)
        if day <= 0 or slot >= len(_SAMPLE_TIMES):
            raise NotFoundError("match", match_id)
        zone = (await self._settings.get_settings(user_id)).zone
        item = _sample(date.fromordinal(day), slot, zone)
        keeper = LineupPlayer(name="Sample Keeper", position="Goalkeeper", shirt_number=1)
        return MatchSheet(
            match=item,
            home_ht_goals=None,
            away_ht_goals=None,
            home=SideLineup(team=item.home, coach="Sample Coach", starters=(keeper,)),
            away=SideLineup(team=item.away),
        )

    async def forecast_day(self, user_id: int, day: date) -> tuple[ForecastLine, ...]:
        """See :meth:`app.services.interfaces.MatchesService.forecast_day`."""
        items = await self.list_matches(user_id, day, day)
        return tuple(
            ForecastLine(match=item, forecast=sample_forecast(item.match_id)) for item in items
        )

    async def forecast_match(self, user_id: int, match_id: int) -> ForecastLine:
        """See :meth:`app.services.interfaces.MatchesService.forecast_match`."""
        sheet = await self.get_match(user_id, match_id)
        return ForecastLine(match=sheet.match, forecast=sample_forecast(match_id))


def sample_forecast(match_id: int) -> MatchForecast:
    """The same plausible forecast for every sample match."""
    groups = (
        (Market.match_result(), _SAMPLE_RESULT),
        (Market.totals(2.5), _SAMPLE_TOTALS),
        (Market.btts(), _SAMPLE_BTTS),
    )
    return MatchForecast(
        match_id=match_id,
        status=ForecastStatus.READY,
        chances=tuple(
            OutcomeChance(market=market, selection=selection, probability=probability)
            for market, pairs in groups
            for selection, probability in pairs
        ),
        scores=(
            ScoreProbability(home_goals=1, away_goals=1, probability=0.12),
            ScoreProbability(home_goals=1, away_goals=0, probability=0.10),
        ),
        team_matches=_SAMPLE_TEAM_MATCHES,
        confidence=ConfidenceLevel.MEDIUM,
    )
