"""Quiet windows, user schedules, defaults and the local-day to UTC conversion."""

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from app.db.enums import QueryScope, SignalSource
from app.pipeline.profile import DEFAULT_SETTINGS, default_request, load_profile, utc_bounds
from app.pipeline.types import QuietWindow, UserSchedule
from app.services.interfaces import (
    BALANCED_PRESET,
    DEFAULT_MARKETS,
    DEFAULT_MAX_SIGNALS,
    DEFAULT_PREMATCH_MINUTES,
    DEFAULT_QUIET_HOURS,
    DEFAULT_SEND_TIME,
    DEFAULT_SPORTS,
    DEFAULT_TIMEZONE,
)

TASHKENT = ZoneInfo("Asia/Tashkent")


def test_quiet_window_inside_one_day() -> None:
    window = QuietWindow(time(1, 0), time(5, 0))
    assert window.contains(time(1, 0))
    assert window.contains(time(4, 59))
    assert not window.contains(time(5, 0))
    assert not window.contains(time(0, 59))


def test_quiet_window_wrapping_midnight() -> None:
    window = QuietWindow(time(23, 0), time(7, 0))
    assert window.contains(time(23, 30))
    assert window.contains(time(2, 0))
    assert not window.contains(time(7, 0))
    assert not window.contains(time(12, 0))


def test_schedule_uses_the_users_zone() -> None:
    schedule = UserSchedule(
        user_id=1, zone=TASHKENT, send_time=time(9), quiet=QuietWindow(time(0), time(8))
    )
    night = datetime(2026, 10, 21, 20, 0, tzinfo=UTC)  # 01:00 next day in Tashkent
    noon = datetime(2026, 10, 21, 7, 0, tzinfo=UTC)  # 12:00 in Tashkent
    assert schedule.is_quiet(night)
    assert not schedule.is_quiet(noon)
    assert schedule.local_day(night) == date(2026, 10, 22)
    assert not UserSchedule(user_id=1, zone=TASHKENT, send_time=time(9)).is_quiet(night)


def test_utc_bounds_of_local_days() -> None:
    start, end = utc_bounds(TASHKENT, date(2026, 10, 21), date(2026, 10, 22))
    assert start == datetime(2026, 10, 20, 19, 0, tzinfo=UTC)
    assert end == datetime(2026, 10, 22, 19, 0, tzinfo=UTC)


def test_pipeline_defaults_equal_the_service_defaults() -> None:
    s = DEFAULT_SETTINGS
    assert {sport.value for sport in DEFAULT_SPORTS} == set(s.sports)
    assert {market.value for market in DEFAULT_MARKETS} == set(s.markets)
    assert (s.min_prob, s.min_ev, s.odds_min, s.odds_max) == (
        BALANCED_PRESET.min_probability,
        BALANCED_PRESET.min_ev,
        BALANCED_PRESET.odds_min,
        BALANCED_PRESET.odds_max,
    )
    assert s.max_signals == DEFAULT_MAX_SIGNALS
    assert s.send_time == DEFAULT_SEND_TIME
    assert s.prematch_minutes == DEFAULT_PREMATCH_MINUTES
    assert (s.quiet_hours_start, s.quiet_hours_end) == (
        DEFAULT_QUIET_HOURS.start,
        DEFAULT_QUIET_HOURS.end,
    )


async def test_unknown_user_gets_defaults_and_a_daily_request(sessions) -> None:  # type: ignore[no-untyped-def]
    async with sessions() as session:
        profile = await load_profile(session, 5)
    assert profile.zone.key == DEFAULT_TIMEZONE
    assert profile.settings.user_id == 5
    assert profile.schedule.send_time == DEFAULT_SEND_TIME

    request = default_request(profile, date(2026, 10, 21), date(2026, 10, 21))

    assert request.scope is QueryScope.ALL
    assert request.source is SignalSource.DAILY
    assert request.max_signals == DEFAULT_MAX_SIGNALS
    assert request.thresholds.min_prob == BALANCED_PRESET.min_probability
    assert request.start == datetime(2026, 10, 20, 19, 0, tzinfo=UTC)
