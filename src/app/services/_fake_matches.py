"""In-memory :class:`~app.services.interfaces.MatchesService` for bot development and tests.

Two sample matches on every local day (``19:00`` and ``21:30`` in the user's zone), ids derived
from the date so the same day always yields the same ids. Production wiring replaces this
with the live Football-Data.org service whenever ``FOOTBALL_DATA_ORG_KEY`` is set, in both
backends (see ``app.main``).
"""

from datetime import date, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from app.services.errors import NotFoundError
from app.services.interfaces import (
    LineupPlayer,
    MatchItem,
    MatchSheet,
    MatchState,
    SettingsService,
    SideLineup,
    check_match_days,
)

_SAMPLE_TIMES: Final = (time(19, 0), time(21, 30))
_SAMPLE_TEAMS: Final = (("Arsenal FC", "Chelsea FC"), ("Real Madrid CF", "FC Barcelona"))
_IDS_PER_DAY: Final = 10
_ZONE: Final = ZoneInfo("UTC")


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
