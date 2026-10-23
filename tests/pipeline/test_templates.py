"""Scheduled templates (F7.4): due detection, exactly-once delivery, quiet hours, isolation."""

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.db.enums import DateMode, QueryScope
from app.pipeline.analyze import AnalysisEngine
from app.pipeline.ledger import MAX_ATTEMPTS, LedgerKey, backoff
from app.pipeline.profile import DEFAULT_SETTINGS, UserProfile
from app.pipeline.provider import AUTOMATIC
from app.pipeline.templates import (
    HORIZON_LAST_DAY,
    JOB,
    PAST_DETAIL,
    due_day,
    resolve_dates,
    runnable_window,
    template_request,
)
from app.pipeline.templates import run_due_templates as run_templates
from app.services.interfaces import (
    AnalysisQuery,
    DateSpec,
    QuietHours,
    TemplateDraft,
    TemplateSchedule,
    horizon_end,
)
from tests.pipeline.doubles import Crash, RecordingDelivery, draft_query, ids
from tests.pipeline.harness import Env

TASHKENT = ZoneInfo("Asia/Tashkent")
NEW_YORK = ZoneInfo("America/New_York")
AUCKLAND = ZoneInfo("Pacific/Auckland")
ALL_DAYS = tuple(range(1, 8))
USER = 3003
CATCH_UP = timedelta(hours=12)
PERMISSIVE = {"min_probability": 0.30, "min_ev": 0.0}


def profile(zone: ZoneInfo = TASHKENT) -> UserProfile:
    return UserProfile(
        user_id=1,
        zone=zone,
        settings=replace(DEFAULT_SETTINGS, user_id=1),
        bank=None,
        unit=None,
        watch_league_ids=frozenset(),
        watch_team_ids=frozenset(),
    )


def at_utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# ------------------------------------------------------------------------------ dates
def _saved(spec: DateSpec) -> object:
    return draft_query(
        date_mode=DateMode(spec.mode.value),
        days=spec.days,
        date_from=spec.date_from,
        date_to=spec.date_to,
    )


def _specs() -> list[DateSpec]:
    return [
        DateSpec.today(),
        DateSpec.tomorrow(),
        DateSpec.weekend(),
        *(DateSpec.next_days(n) for n in (1, 2, 7, 14)),
        DateSpec.between(date(2026, 10, 3), date(2026, 10, 9)),
    ]


@pytest.mark.parametrize("offset", range(14))  # two full weeks: every weekday twice
def test_resolving_dates_agrees_with_the_contract_for_every_mode(offset: int) -> None:
    today = date(2026, 10, 19) + timedelta(days=offset)
    for spec in _specs():
        window = spec.resolve(today)
        assert resolve_dates(_saved(spec), today) == (window.date_from, window.date_to), spec  # type: ignore[arg-type]


def test_the_horizon_is_the_contracts() -> None:
    today = date(2026, 10, 21)
    assert today + timedelta(days=HORIZON_LAST_DAY) == horizon_end(today)


def test_the_weekend_is_what_remains_of_it() -> None:
    saturday = date(2026, 10, 24)
    query = draft_query(date_mode=DateMode.WEEKEND)
    assert resolve_dates(query, date(2026, 10, 21)) == (saturday, date(2026, 10, 25))
    assert resolve_dates(query, date(2026, 10, 24)) == (saturday, date(2026, 10, 25))
    assert resolve_dates(query, date(2026, 10, 25)) == (date(2026, 10, 25), date(2026, 10, 25))
    assert resolve_dates(query, date(2026, 10, 26)) == (date(2026, 10, 31), date(2026, 11, 1))


def test_a_scheduled_run_never_looks_back_nor_beyond_the_horizon() -> None:
    today = date(2026, 10, 21)
    past = draft_query(
        date_mode=DateMode.RANGE, date_from=date(2026, 10, 1), date_to=date(2026, 10, 5)
    )
    straddling = draft_query(
        date_mode=DateMode.RANGE, date_from=date(2026, 10, 18), date_to=date(2026, 10, 23)
    )
    far = draft_query(
        date_mode=DateMode.RANGE, date_from=date(2026, 11, 1), date_to=date(2026, 11, 20)
    )
    assert runnable_window(past, today) is None
    assert runnable_window(straddling, today) == (today, date(2026, 10, 23))
    assert runnable_window(far, today) == (date(2026, 11, 1), date(2026, 11, 3))
    beyond = draft_query(
        date_mode=DateMode.RANGE, date_from=date(2026, 11, 5), date_to=date(2026, 11, 6)
    )
    assert runnable_window(beyond, today) is None  # past the 14-day horizon


# ------------------------------------------------------------------------------ due day
def due(
    zone: ZoneInfo, now: datetime, at: time, weekdays: tuple[int, ...] = ALL_DAYS
) -> date | None:
    query = draft_query(schedule_weekdays=weekdays, schedule_at=at)
    return due_day(profile(zone), query, now, CATCH_UP)


def test_an_unscheduled_query_is_never_due() -> None:
    assert due_day(profile(), draft_query(), at_utc(2026, 10, 21, 12), CATCH_UP) is None


def test_due_exactly_from_the_scheduled_local_minute() -> None:
    nine = time(9, 0)  # 04:00 UTC in Tashkent
    assert due(TASHKENT, at_utc(2026, 10, 21, 3, 59), nine) is None
    assert due(TASHKENT, at_utc(2026, 10, 21, 4, 0), nine) == date(2026, 10, 21)
    assert due(TASHKENT, at_utc(2026, 10, 21, 9, 0), nine) == date(2026, 10, 21)


def test_the_catch_up_window_is_inclusive_at_twelve_hours() -> None:
    nine = time(9, 0)
    assert due(TASHKENT, at_utc(2026, 10, 21, 16, 0), nine) == date(2026, 10, 21)  # 12 h exactly
    assert due(TASHKENT, at_utc(2026, 10, 21, 16, 1), nine) is None


def test_only_the_scheduled_weekdays_are_due() -> None:
    thursday_only = (4,)
    nine = time(9, 0)
    assert due(TASHKENT, at_utc(2026, 10, 21, 5), nine, thursday_only) is None  # Wednesday
    assert due(TASHKENT, at_utc(2026, 10, 22, 5), nine, thursday_only) == date(2026, 10, 22)


def test_a_run_that_slipped_over_midnight_belongs_to_the_day_it_was_due() -> None:
    late = time(23, 50)
    now = at_utc(2026, 10, 21, 19, 10)  # 00:10 on Thursday in Tashkent
    assert due(TASHKENT, now, late, (3,)) == date(2026, 10, 21)  # Wednesday's run, caught up
    assert due(TASHKENT, now, late, (4,)) is None  # Thursday's has not happened yet
    assert due(TASHKENT, at_utc(2026, 10, 21, 18, 49), late) is None  # 23:49 locally


def test_a_time_that_does_not_exist_runs_once_after_the_clocks_jump() -> None:
    # New York, 2027-03-14: 02:00 jumps to 03:00, so 02:30 does not exist -> 03:30 EDT.
    half_past_two = time(2, 30)
    assert due(NEW_YORK, at_utc(2027, 3, 14, 7, 29), half_past_two) is None
    assert due(NEW_YORK, at_utc(2027, 3, 14, 7, 30), half_past_two) == date(2027, 3, 14)


def test_a_time_that_happens_twice_is_due_from_its_first_occurrence() -> None:
    # New York, 2026-11-01: 01:30 happens at 05:30 UTC (EDT) and again at 06:30 UTC (EST).
    half_past_one = time(1, 30)
    assert due(NEW_YORK, at_utc(2026, 11, 1, 5, 29), half_past_one) is None
    assert due(NEW_YORK, at_utc(2026, 11, 1, 5, 30), half_past_one) == date(2026, 11, 1)
    assert due(NEW_YORK, at_utc(2026, 11, 1, 6, 30), half_past_one) == date(2026, 11, 1)


def test_the_local_day_of_a_far_east_zone_is_ahead_of_utc() -> None:
    # Auckland is +12 (+13 in summer): 09:00 on 2026-09-27 (DST has just begun) is 20:00 UTC.
    nine = time(9, 0)
    assert due(AUCKLAND, at_utc(2026, 9, 26, 19, 59), nine) is None
    assert due(AUCKLAND, at_utc(2026, 9, 26, 20, 0), nine) == date(2026, 9, 27)


# ------------------------------------------------------------------------------ request
def test_the_request_is_the_templates_own_automatic_query() -> None:
    query = draft_query(
        scope=QueryScope.TEAMS, scope_ids=(5, 6), max_signals=3, per_day=True, include_no_value=True
    )

    request = template_request(profile(), query, (date(2026, 10, 21), date(2026, 10, 22)))

    assert request.purpose is AUTOMATIC
    assert request.skip_retro
    assert request.team_ids == frozenset({5, 6})
    assert request.league_ids == request.fixture_ids == frozenset()
    assert (request.max_signals, request.per_day, request.include_no_value) == (3, True, True)
    assert request.start == at_utc(2026, 10, 20, 19)  # Tashkent midnight of the 21st
    assert request.end == at_utc(2026, 10, 22, 19)


# ------------------------------------------------------------------------------ the job
async def schedule(  # noqa: PLR0913 - one switch per thing a test may vary
    env: Env,
    name: str = "Ertalab",
    *,
    weekdays: frozenset[int] = frozenset(ALL_DAYS),
    at: time = time(11, 0),
    dates: DateSpec | None = None,
    user_id: int = USER,
) -> int:
    """Save a scheduled template (permissive thresholds) and return its id."""
    services = env.system.services
    settings = await services.settings.get_settings(user_id)
    await services.settings.update_settings(user_id, replace(settings, quiet_hours=None))
    query = AnalysisQuery(dates=dates or DateSpec.next_days(2), **PERMISSIVE)
    saved = await services.templates.save_template(
        user_id,
        TemplateDraft(name=name, query=query, schedule=TemplateSchedule(weekdays=weekdays, at=at)),
    )
    return saved.template_id


async def test_a_due_template_is_delivered_once_with_its_name(seeded_env: Env) -> None:
    delivery = RecordingDelivery()
    template_id = await schedule(seeded_env)  # 11:00 Tashkent = 06:00 UTC = "now"

    first = await run_templates(seeded_env.ctx, delivery)
    second = await run_templates(seeded_env.ctx, delivery)
    seeded_env.clock.advance(timedelta(minutes=45))
    third = await run_templates(seeded_env.ctx, delivery)

    assert first.sent == ((USER, template_id),)
    assert second.sent == third.sent == ()
    assert delivery.labels() == ["Ertalab"]
    _, message, key = delivery.digests[0]
    assert message.outcome.cards  # the stored query went through the one analysis path
    assert message.day == date(2026, 10, 21)
    assert key == LedgerKey(JOB, date(2026, 10, 21), USER, str(template_id))
    assert await seeded_env.ctx.ledger.is_done(key)


async def test_through_the_notifier_the_digest_carries_the_template_name(
    seeded_env: Env,
) -> None:
    await schedule(seeded_env, "Hafta oxiri")

    await run_templates(seeded_env.ctx, seeded_env.system.delivery)

    [sent] = [n for n in seeded_env.notifier.sent if n.method == "daily_digest"]
    assert sent.user_id == USER
    assert sent.payload.template_name == "Hafta oxiri"  # type: ignore[attr-defined]
    assert sent.payload.yesterday is None  # type: ignore[attr-defined]  # the morning tally is the digest's
    assert sent.key is not None
    assert (sent.key.job, sent.key.day, sent.key.user_id) == (JOB, date(2026, 10, 21), USER)


async def test_a_template_runs_once_per_local_day_and_again_the_next_day(seeded_env: Env) -> None:
    delivery = RecordingDelivery()
    await schedule(seeded_env)

    for minutes in (0, 1, 30, 360):
        seeded_env.clock.now = datetime(2026, 10, 21, 6, 0, tzinfo=UTC) + timedelta(minutes=minutes)
        await run_templates(seeded_env.ctx, delivery)
    seeded_env.clock.now = datetime(2026, 10, 22, 6, 0, tzinfo=UTC)
    await run_templates(seeded_env.ctx, delivery)

    assert [d.day for _, d, _ in delivery.digests] == [date(2026, 10, 21), date(2026, 10, 22)]


async def test_the_weekday_selection_is_honoured(seeded_env: Env) -> None:
    delivery = RecordingDelivery()
    await schedule(seeded_env, weekdays=frozenset({4}))  # Thursday only; the clock says Wednesday

    wednesday = await run_templates(seeded_env.ctx, delivery)
    seeded_env.clock.now = datetime(2026, 10, 22, 6, 0, tzinfo=UTC)
    thursday = await run_templates(seeded_env.ctx, delivery)

    assert wednesday.sent == ()
    assert len(thursday.sent) == 1


async def test_a_template_is_held_through_quiet_hours_then_sent(seeded_env: Env) -> None:
    delivery = RecordingDelivery()
    template_id = await schedule(seeded_env)
    services = seeded_env.system.services
    settings = await services.settings.get_settings(USER)
    quiet = QuietHours(time(10, 0), time(12, 0))  # 11:00 local falls inside
    await services.settings.update_settings(USER, replace(settings, quiet_hours=quiet))

    held = await run_templates(seeded_env.ctx, delivery)
    seeded_env.clock.now = datetime(2026, 10, 21, 6, 59, tzinfo=UTC)  # 11:59 local
    still_held = await run_templates(seeded_env.ctx, delivery)
    seeded_env.clock.now = datetime(2026, 10, 21, 7, 0, tzinfo=UTC)  # 12:00: the end is outside
    released = await run_templates(seeded_env.ctx, delivery)

    assert held.held == still_held.held == ((USER, template_id),)
    assert held.sent == ()
    assert released.sent == ((USER, template_id),)
    assert delivery.digests[0][2].day == date(2026, 10, 21)  # keyed by the day it was due


async def test_a_held_template_that_misses_the_catch_up_window_is_dropped(
    seeded_env: Env,
) -> None:
    delivery = RecordingDelivery()
    await schedule(seeded_env)
    services = seeded_env.system.services
    settings = await services.settings.get_settings(USER)
    all_day_quiet = QuietHours(time(10, 0), time(23, 59))
    await services.settings.update_settings(USER, replace(settings, quiet_hours=all_day_quiet))
    await run_templates(seeded_env.ctx, delivery)

    seeded_env.clock.now = datetime(2026, 10, 21, 18, 59, tzinfo=UTC)  # 23:59 local, 13 h late
    late = await run_templates(seeded_env.ctx, delivery)

    assert late.sent == late.held == ()
    assert delivery.digests == []


async def test_a_template_slipped_over_midnight_is_sent_for_the_day_it_was_due(
    seeded_env: Env,
) -> None:
    delivery = RecordingDelivery()
    template_id = await schedule(seeded_env, at=time(23, 50), weekdays=frozenset({3}))  # Wednesday
    seeded_env.clock.now = datetime(2026, 10, 21, 19, 10, tzinfo=UTC)  # 00:10 Thursday

    caught_up = await run_templates(seeded_env.ctx, delivery)
    again = await run_templates(seeded_env.ctx, delivery)

    assert caught_up.sent == ((USER, template_id),)
    assert again.sent == ()
    assert delivery.digests[0][2].day == date(2026, 10, 21)
    assert delivery.digests[0][1].day == date(2026, 10, 22)  # the message is about today


async def test_across_the_ambiguous_hour_of_a_clock_change_it_runs_exactly_once(
    seeded_env: Env,
) -> None:
    delivery = RecordingDelivery()
    services = seeded_env.system.services
    settings = await services.settings.get_settings(USER)
    await services.settings.update_settings(
        USER, replace(settings, timezone="America/New_York", quiet_hours=None)
    )
    await schedule(seeded_env, at=time(1, 30), dates=DateSpec.today())

    # 2026-11-01: 01:30 happens at 05:30 UTC and again at 06:30 UTC. Tick every 10 minutes.
    moment = datetime(2026, 11, 1, 4, 0, tzinfo=UTC)
    while moment < datetime(2026, 11, 1, 9, 0, tzinfo=UTC):
        seeded_env.clock.now = moment
        await run_templates(seeded_env.ctx, delivery)
        moment += timedelta(minutes=10)

    assert len(delivery.digests) == 1
    assert delivery.digests[0][2].day == date(2026, 11, 1)


async def test_the_missing_hour_of_a_clock_change_still_runs_that_day(seeded_env: Env) -> None:
    delivery = RecordingDelivery()
    services = seeded_env.system.services
    settings = await services.settings.get_settings(USER)
    await services.settings.update_settings(
        USER, replace(settings, timezone="America/New_York", quiet_hours=None)
    )
    await schedule(seeded_env, at=time(2, 30), dates=DateSpec.today())

    moment = datetime(2027, 3, 14, 6, 0, tzinfo=UTC)
    while moment < datetime(2027, 3, 14, 12, 0, tzinfo=UTC):
        seeded_env.clock.now = moment
        await run_templates(seeded_env.ctx, delivery)
        moment += timedelta(minutes=10)

    assert [key.day for _, _, key in delivery.digests] == [date(2027, 3, 14)]
    assert delivery.digests[0][1].day == date(2027, 3, 14)


async def test_an_unscheduled_template_never_runs(seeded_env: Env) -> None:
    delivery = RecordingDelivery()
    services = seeded_env.system.services
    await schedule(seeded_env, "Jadvalli")
    await services.templates.save_template(
        USER, TemplateDraft(name="Jadvalsiz", query=AnalysisQuery(dates=DateSpec.today()))
    )

    await run_templates(seeded_env.ctx, delivery)

    assert delivery.labels() == ["Jadvalli"]


async def test_a_template_whose_dates_have_passed_sends_nothing_and_is_closed(
    seeded_env: Env,
) -> None:
    delivery = RecordingDelivery()
    past = DateSpec.between(date(2026, 10, 1), date(2026, 10, 5))
    template_id = await schedule(seeded_env, dates=past)

    report = await run_templates(seeded_env.ctx, delivery)

    assert report.sent == ((USER, template_id),)
    assert delivery.digests == []
    entry = await seeded_env.ctx.ledger.get(
        LedgerKey(JOB, date(2026, 10, 21), USER, str(template_id))
    )
    assert entry is not None
    assert entry.detail == PAST_DETAIL


async def test_one_failing_template_does_not_stop_the_others(seeded_env: Env) -> None:
    delivery = RecordingDelivery()
    delivery.fail_if = lambda m: ConnectionError("telegram") if m.label == "Buzuq" else None
    broken = await schedule(seeded_env, "Buzuq")
    fine = await schedule(seeded_env, "Soz")

    report = await run_templates(seeded_env.ctx, delivery)

    assert ids(report.failed) == [broken]
    assert ids(report.sent) == [fine]
    assert delivery.labels() == ["Soz"]
    entry = await seeded_env.ctx.ledger.get(LedgerKey(JOB, date(2026, 10, 21), USER, str(broken)))
    assert entry is not None
    assert (entry.status, entry.attempts) == ("failed", 1)


async def test_a_failed_template_backs_off_then_retries_without_analysing_again(
    seeded_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    delivery = RecordingDelivery()
    delivery.answers = [None]  # the bot is not attached yet: not delivered
    runs: list[int] = []
    original = AnalysisEngine.run

    async def counting(self: AnalysisEngine, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        runs.append(1)
        return await original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(AnalysisEngine, "run", counting)
    template_id = await schedule(seeded_env)

    first = await run_templates(seeded_env.ctx, delivery)
    inside_backoff = await run_templates(seeded_env.ctx, delivery)
    seeded_env.clock.advance(backoff(1))
    retried = await run_templates(seeded_env.ctx, delivery)

    assert ids(first.failed) == [template_id]
    assert inside_backoff.sent == inside_backoff.failed == ()
    assert ids(retried.sent) == [template_id]
    assert len(runs) == 1  # the stored analysis was re-sent, not recomputed
    [(_, unconfirmed, _), (_, resent, _)] = delivery.digests
    assert unconfirmed.outcome.cards
    assert [c.record.id for c in unconfirmed.outcome.cards] == [
        c.record.id for c in resent.outcome.cards
    ]


async def test_a_template_gives_up_after_the_attempt_limit_and_alerts(seeded_env: Env) -> None:
    delivery = RecordingDelivery()
    delivery.fail_if = lambda _m: ConnectionError("down")
    await schedule(seeded_env)

    for _ in range(MAX_ATTEMPTS + 2):
        await run_templates(seeded_env.ctx, delivery)
        seeded_env.clock.advance(backoff(MAX_ATTEMPTS))

    assert delivery.digests == []
    assert any("gave up" in message for _, message in delivery.alerts)
    final = await run_templates(seeded_env.ctx, delivery)
    assert final.failed == ()  # exhausted: never claimed again


async def test_concurrent_ticks_deliver_exactly_once(seeded_env: Env) -> None:
    delivery = RecordingDelivery()
    await schedule(seeded_env)

    await asyncio.gather(*(run_templates(seeded_env.ctx, delivery) for _ in range(3)))

    assert len(delivery.digests) == 1


async def test_a_crash_after_sending_resends_only_after_the_lease(seeded_env: Env) -> None:
    delivery = RecordingDelivery()
    calls = {"n": 0}

    def crash_once(_message: object) -> BaseException | None:
        calls["n"] += 1
        return Crash("killed") if calls["n"] == 1 else None

    delivery.fail_if = crash_once
    template_id = await schedule(seeded_env)

    with pytest.raises(Crash):
        await run_templates(seeded_env.ctx, delivery)
    within_lease = await run_templates(seeded_env.ctx, delivery)
    seeded_env.clock.advance(timedelta(minutes=16))
    after_lease = await run_templates(seeded_env.ctx, delivery)

    assert within_lease.sent == ()  # someone may still be sending it
    assert ids(after_lease.sent) == [template_id]  # at least once, deduplicated by the key


async def test_users_get_only_their_own_templates(seeded_env: Env) -> None:
    delivery = RecordingDelivery()
    mine = await schedule(seeded_env, "Meniki", user_id=USER)
    other = await schedule(seeded_env, "Boshqaniki", user_id=USER + 1)

    report = await run_templates(seeded_env.ctx, delivery)

    assert sorted(report.sent) == [(USER, mine), (USER + 1, other)]
    by_user = {user: message.label for user, message, _ in delivery.digests}
    assert by_user == {USER: "Meniki", USER + 1: "Boshqaniki"}


async def test_a_broken_user_row_does_not_stop_the_other_users(
    seeded_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    delivery = RecordingDelivery()
    await schedule(seeded_env, user_id=USER)
    good = await schedule(seeded_env, user_id=USER + 1)
    from app.pipeline import templates  # noqa: PLC0415

    original = templates.load_profile

    async def flaky(session: object, user_id: int) -> object:
        if user_id == USER:
            msg = "corrupt settings row"
            raise ValueError(msg)
        return await original(session, user_id)  # type: ignore[arg-type]

    monkeypatch.setattr(templates, "load_profile", flaky)

    report = await run_templates(seeded_env.ctx, delivery)

    assert report.sent == ((USER + 1, good),)
    errors = await seeded_env.ctx.ledger.recent_errors()
    assert any("corrupt settings row" in message for _, _, message in errors)
