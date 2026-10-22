"""A user's stored profile as the pipeline uses it, and how it becomes an analysis request.

Settings are stored per user; a user without a settings row gets the documented defaults
(the "Balanced" preset, decision #8). Nothing here talks to the provider.
"""

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Final
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import QueryScope, RiskProfile, SignalSource, SortOrder, WatchKind
from app.db.repo import UserSettingsRecord, WatchRecord, get_user, get_user_settings, list_watchlist
from app.domain import MarketType, Sport
from app.pipeline.provider import AUTOMATIC, Purpose
from app.pipeline.types import AnalysisRequest, QuietWindow, UserSchedule

DEFAULT_TIMEZONE: Final = "Asia/Tashkent"
DEFAULT_SETTINGS: Final = UserSettingsRecord(
    user_id=0,
    sports=(Sport.FOOTBALL.value,),
    leagues=(),
    markets=(MarketType.MATCH_RESULT.value, MarketType.TOTALS.value),
    max_signals=10,
    min_prob=0.45,
    min_ev=0.05,
    odds_min=1.50,
    odds_max=3.00,
    risk_profile=RiskProfile.BALANCED,
    send_time=time(9, 0),
    prematch_minutes=60,
    quiet_hours_start=time(0, 0),
    quiet_hours_end=time(8, 0),
)
"""Must equal the service layer's defaults (a test compares them)."""


@dataclass(frozen=True, slots=True)
class UserProfile:
    """Everything the pipeline knows about one user."""

    user_id: int
    zone: ZoneInfo
    settings: UserSettingsRecord
    bank: Decimal | None
    unit: Decimal | None
    watch_league_ids: frozenset[int]
    watch_team_ids: frozenset[int]
    watches: tuple[WatchRecord, ...] = ()

    @property
    def schedule(self) -> UserSchedule:
        """When the user wants to be messaged."""
        s = self.settings
        quiet = (
            None
            if s.quiet_hours_start is None or s.quiet_hours_end is None
            else QuietWindow(s.quiet_hours_start, s.quiet_hours_end)
        )
        return UserSchedule(
            user_id=self.user_id,
            zone=self.zone,
            send_time=s.send_time,
            quiet=quiet,
            prematch_minutes=s.prematch_minutes,
        )


async def load_profile(session: AsyncSession, user_id: int) -> UserProfile:
    """The stored profile of ``user_id``; unknown users and missing settings get defaults."""
    user = await get_user(session, user_id)
    stored = await get_user_settings(session, user_id)
    watches = await list_watchlist(session, user_id)
    return UserProfile(
        user_id=user_id,
        zone=ZoneInfo(DEFAULT_TIMEZONE if user is None else user.timezone),
        settings=stored or _with_user(DEFAULT_SETTINGS, user_id),
        bank=None if user is None else user.bank,
        unit=None if user is None else user.unit,
        watch_league_ids=frozenset(w.entity_id for w in watches if w.kind is WatchKind.LEAGUE),
        watch_team_ids=frozenset(w.entity_id for w in watches if w.kind is WatchKind.TEAM),
        watches=tuple(watches),
    )


def _with_user(settings: UserSettingsRecord, user_id: int) -> UserSettingsRecord:
    return replace(settings, user_id=user_id)


def utc_bounds(zone: ZoneInfo, first: date, last: date) -> tuple[datetime, datetime]:
    """Half-open UTC interval covering the local days ``first..last`` (DST safe)."""
    start = datetime.combine(first, time(0), tzinfo=zone)
    end = datetime.combine(last + timedelta(days=1), time(0), tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)


def default_request(
    profile: UserProfile,
    first: date,
    last: date,
    *,
    source: SignalSource = SignalSource.DAILY,
    purpose: Purpose = AUTOMATIC,
) -> AnalysisRequest:
    """The user's own default analysis of the local days ``first..last`` (the daily digest)."""
    s = profile.settings
    start, end = utc_bounds(profile.zone, first, last)
    return AnalysisRequest(
        user_id=profile.user_id,
        start=start,
        end=end,
        zone=profile.zone,
        sports=frozenset(Sport(value) for value in s.sports),
        markets=frozenset(MarketType(value) for value in s.markets),
        min_probability=s.min_prob,
        min_ev=s.min_ev,
        odds_min=s.odds_min,
        odds_max=s.odds_max,
        scope=QueryScope.ALL,
        default_league_ids=frozenset(s.leagues),
        watch_league_ids=profile.watch_league_ids,
        watch_team_ids=profile.watch_team_ids,
        max_signals=s.max_signals,
        sort=SortOrder.EV,
        source=source,
        bank=profile.bank,
        unit=profile.unit,
        prematch_minutes=s.prematch_minutes,
        purpose=purpose,
    )
