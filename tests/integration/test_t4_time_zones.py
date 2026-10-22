"""T4 QA (7): one digest per user per local day across zones and DST, and quiet hours.

The expected UTC instants of every send are written down by hand from the zones' rules
(New York leaves daylight time on 2026-11-01 and enters it on 2027-03-14; Auckland leaves it
on 2027-04-04), then compared with the instants the digest job really fires at when it is
ticked on a half-hour grid.
"""

from dataclasses import replace
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.db.repo import upsert_live
from app.domain import BOOKMAKER_1XBET, HistoryBatch, OddsKind, Selection
from app.pipeline.config import PipelineConfig
from app.pipeline.daily import run_due_digests
from app.pipeline.prematch import run_due_recalcs
from app.pipeline.settle import run_settlement
from app.pipeline.types import QuietWindow
from app.scheduler import JOB_REFRESH, PipelineJobs, execute_job, install
from app.services.interfaces import DateSpec, QuietHours, ResultMark
from tests.integration.t4_support import (
    USER,
    FlakyNotifier,
    add_signal,
    analyse_as,
    finished,
    make_fixture,
    open_bare,
    permissive,
    snapshot,
    utc,
    x12,
)
from tests.integration.test_t4_hand_league import (
    FIXTURE,
    KICKOFF,
    MODEL_WEIGHT,
    ONE_X_TWO,
    WORLD_A,
    HandModels,
    batch,
)

TICK = timedelta(minutes=30)


async def digests_over(
    tmp_path: Path, zone: str, send: time, start: datetime, end: datetime
) -> list[datetime]:
    """The UTC instants (on a half-hour grid) at which the user's digest was delivered."""
    notifier = FlakyNotifier()
    sends: list[datetime] = []
    async with open_bare(
        tmp_path / f"{zone.replace('/', '_')}.db", None, notifier=notifier, now=start
    ) as bare:
        services = bare.system.services
        view = await services.settings.get_settings(USER)
        await services.settings.update_settings(
            USER, replace(view, timezone=zone, send_time=send, quiet_hours=None)
        )
        while bare.clock() < end:
            before = notifier.count("daily_digest")
            await run_due_digests(bare.ctx, bare.system.delivery)
            if notifier.count("daily_digest") > before:
                sends.append(bare.clock())
            bare.clock.advance(TICK)
    return sends


NINE = time(9, 0)
SCENARIOS = {
    # 09:00 in Asia/Tashkent (UTC+5, no DST) is 04:00 UTC every day
    "tashkent": (
        "Asia/Tashkent",
        NINE,
        utc(2026, 10, 29, 18),
        utc(2026, 11, 3),
        [utc(2026, 10, 30, 4), utc(2026, 10, 31, 4), utc(2026, 11, 1, 4), utc(2026, 11, 2, 4)],
    ),
    # New York leaves daylight time at 06:00 UTC on 2026-11-01: 09:00 EDT = 13:00 UTC until
    # the 31st, 09:00 EST = 14:00 UTC from the 1st
    "new-york-fall-back": (
        "America/New_York",
        NINE,
        utc(2026, 10, 30, 2),
        utc(2026, 11, 3),
        [utc(2026, 10, 30, 13), utc(2026, 10, 31, 13), utc(2026, 11, 1, 14), utc(2026, 11, 2, 14)],
    ),
    # ... and enters it at 07:00 UTC on 2027-03-14: 09:00 EST = 14:00 UTC, then 13:00 UTC
    "new-york-spring-forward": (
        "America/New_York",
        NINE,
        utc(2027, 3, 12, 3),
        utc(2027, 3, 16),
        [utc(2027, 3, 12, 14), utc(2027, 3, 13, 14), utc(2027, 3, 14, 13), utc(2027, 3, 15, 13)],
    ),
    # Auckland is UTC+13 in October: local 09:00 is 20:00 UTC of the previous UTC day
    "auckland-day-boundary": (
        "Pacific/Auckland",
        NINE,
        utc(2026, 10, 30, 10),
        utc(2026, 11, 3),
        [utc(2026, 10, 30, 20), utc(2026, 10, 31, 20), utc(2026, 11, 1, 20), utc(2026, 11, 2, 20)],
    ),
    # NZ daylight time ends at 14:00 UTC on 2027-04-03 (local 03:00 on the 4th): 09:00 NZDT is
    # 20:00 UTC, 09:00 NZST is 21:00 UTC
    "auckland-fall-back": (
        "Pacific/Auckland",
        NINE,
        utc(2027, 4, 2, 10),
        utc(2027, 4, 6),
        [utc(2027, 4, 2, 20), utc(2027, 4, 3, 21), utc(2027, 4, 4, 21), utc(2027, 4, 5, 21)],
    ),
    # 02:30 does not exist in New York on 2027-03-14 (the clocks jump to 03:00): the digest
    # is still sent once that day (the wall time resolves to 03:30 EDT)
    "new-york-skipped-wall-time": (
        "America/New_York",
        time(2, 30),
        utc(2027, 3, 13),
        utc(2027, 3, 16),
        [utc(2027, 3, 13, 7, 30), utc(2027, 3, 14, 7, 30), utc(2027, 3, 15, 6, 30)],
    ),
    # 01:30 happens twice in New York on 2026-11-01: sent at the first one, not again at the second
    "new-york-repeated-wall-time": (
        "America/New_York",
        time(1, 30),
        utc(2026, 10, 31),
        utc(2026, 11, 3),
        [utc(2026, 10, 31, 5, 30), utc(2026, 11, 1, 5, 30), utc(2026, 11, 2, 6, 30)],
    ),
}


@pytest.mark.slow
@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_exactly_one_digest_per_local_day_at_the_right_instant(
    tmp_path: Path, name: str
) -> None:
    zone, send, start, end, expected = SCENARIOS[name]
    sends = await digests_over(tmp_path, zone, send, start, end)

    assert sends == expected


# ------------------------------------------------------------------------------ quiet hours
@pytest.mark.slow
async def test_a_digest_inside_quiet_hours_waits_for_their_end_across_midnight(
    tmp_path: Path,
) -> None:
    """Quiet 22:00-08:00 (wraps midnight), send time 07:00: sent at 08:00 local, once."""
    notifier = FlakyNotifier()
    async with open_bare(
        tmp_path / "held.db",
        None,
        notifier=notifier,
        now=utc(2026, 10, 21, 0),  # 05:00 Tashkent
    ) as bare:
        services = bare.system.services
        view = await services.settings.get_settings(USER)
        await services.settings.update_settings(
            USER,
            replace(
                view,
                send_time=time(7, 0),
                quiet_hours=QuietHours(time(22, 0), time(8, 0)),
            ),
        )
        held_at, sent_at = [], []
        for _ in range(12 * 12):  # 5-minute ticks for 12 hours
            report = await run_due_digests(bare.ctx, bare.system.delivery)
            if report.held:
                held_at.append(bare.clock())
            if report.sent:
                sent_at.append(bare.clock())
            bare.clock.advance(timedelta(minutes=5))

    assert sent_at == [utc(2026, 10, 21, 3, 0)]  # 08:00 Tashkent, the quiet end (exclusive)
    assert held_at[0] == utc(2026, 10, 21, 2, 0)  # 07:00 local, due but quiet
    assert held_at[-1] == utc(2026, 10, 21, 2, 55)
    assert notifier.count("daily_digest") == 1


@pytest.mark.parametrize(
    ("quiet_end_hour", "sent"),
    [(18, True), (19, True), (20, False)],  # send 07:00; 11 h, 12 h and 13 h of quiet
)
async def test_a_digest_held_longer_than_the_catch_up_window_is_skipped(
    tmp_path: Path, quiet_end_hour: int, sent: bool
) -> None:
    notifier = FlakyNotifier()
    async with open_bare(
        tmp_path / f"catchup{quiet_end_hour}.db", None, notifier=notifier, now=utc(2026, 10, 21, 2)
    ) as bare:
        services = bare.system.services
        view = await services.settings.get_settings(USER)
        await services.settings.update_settings(
            USER,
            replace(
                view,
                send_time=time(7, 0),
                quiet_hours=QuietHours(time(7, 0), time(quiet_end_hour, 0)),
            ),
        )
        for _ in range(16 * 2):
            await run_due_digests(bare.ctx, bare.system.delivery)
            bare.clock.advance(TICK)

    assert (notifier.count("daily_digest") == 1) is sent


QUIET_CASES = [
    # (quiet start, quiet end, is the pre-match message at 19:00 local delivered?)
    pytest.param(time(22, 0), time(8, 0), True, id="wrapping-window-not-containing-19:00"),
    pytest.param(time(19, 0), time(3, 0), False, id="wrapping-window-starting-at-19:00"),
    pytest.param(time(8, 0), time(19, 0), True, id="window-ending-at-19:00-exclusive"),
    pytest.param(time(20, 0), time(19, 30), False, id="wrapping-window-around-19:00"),
    pytest.param(time(18, 59), time(19, 1), False, id="short-window-around-19:00"),
    pytest.param(None, None, True, id="no-quiet-hours"),
]


@pytest.mark.parametrize(("start", "end", "delivered"), QUIET_CASES)
async def test_quiet_hours_drop_the_prematch_message_but_the_recalculation_still_runs(
    tmp_path: Path, start: time | None, end: time | None, delivered: bool
) -> None:
    notifier = FlakyNotifier()
    config = PipelineConfig(model_weight=MODEL_WEIGHT)
    async with open_bare(
        tmp_path / "quiet.db",
        batch(WORLD_A, utc(2026, 10, 21, 3)),
        config=config,
        notifier=notifier,
        now=utc(2026, 10, 21, 6),
    ) as bare:
        object.__setattr__(bare.ctx, "models", HandModels())
        services = bare.system.services
        quiet = None if start is None or end is None else QuietHours(start, end)
        await permissive(
            services,
            USER,
            min_probability=0.45,
            min_ev=0.05,
            odds_min=1.01,
            odds_max=20.0,
            quiet_hours=quiet,
            prematch_minutes=60,
        )
        (signal,) = (await analyse_as(services, USER, DateSpec.today())).signals
        # a newer price: the recalculation must store it even when the message is dropped
        async with bare.sessions.begin() as session:
            await upsert_live(
                session,
                odds=snapshot(
                    FIXTURE,
                    BOOKMAKER_1XBET,
                    ONE_X_TWO,
                    x12(2.30, 4.40, 4.00),
                    kind=OddsKind.RECALC,
                    taken_at=utc(2026, 10, 21, 7),
                ),
            )
        bare.clock.now = KICKOFF - timedelta(minutes=60)  # 19:00 in Asia/Tashkent
        report = await run_due_recalcs(bare.ctx, bare.system.delivery)
        stored = await services.signals.get_signal(USER, signal.signal_id)

    assert (notifier.count("signal_update") == 1) is delivered
    assert report.failed == 0
    assert stored.odds == 2.30  # the numbers were refreshed either way
    assert stored.version == 2
    assert stored.ev == pytest.approx(stored.probability * 2.30 - 1, abs=1e-9)


def minutes(moment: time) -> int:
    return moment.hour * 60 + moment.minute


@given(
    start=st.times(),
    end=st.times(),
    moment=st.times(),
)
def test_quiet_window_membership_matches_a_minute_of_day_oracle(
    start: time, end: time, moment: time
) -> None:
    start, end, moment = (t.replace(second=0, microsecond=0) for t in (start, end, moment))
    if start == end:
        return  # QuietHours rejects an empty span; the window is only fed valid spans
    s, e, m = minutes(start), minutes(end), minutes(moment)
    inside = (s <= m < e) if s < e else (m >= s or m < e)
    assert QuietWindow(start, end).contains(moment) is inside


async def test_a_result_that_arrives_in_quiet_hours_is_stored_but_not_announced(
    tmp_path: Path,
) -> None:
    """Decision 15: result messages are dropped in quiet hours (the default is 00:00-08:00)."""
    fixture = make_fixture("quiet-result", utc(2026, 10, 21, 18))
    notifier = FlakyNotifier()
    async with open_bare(
        tmp_path / "quietresult.db",
        HistoryBatch(fixtures=(fixture,)),
        notifier=notifier,
        now=utc(2026, 10, 21, 17),
    ) as bare:
        record = await add_signal(bare, fixture, ONE_X_TWO, Selection.HOME, 2.0, Decimal(1))
        bare.clock.now = utc(2026, 10, 21, 21)  # 02:00 in Asia/Tashkent
        async with bare.sessions.begin() as session:
            await upsert_live(session, results=[finished(fixture, 1, 0)])
        report = await run_settlement(bare.ctx, bare.system.delivery)
        later = await run_settlement(bare.ctx, bare.system.delivery)
        view = await bare.system.services.signals.get_signal(USER, record.id)

    assert (report.settled, report.notified) == (1, 0)
    assert later.settled == 0
    assert view.result is ResultMark.WON  # still the books are right
    assert notifier.count("result_settled") == 0


async def test_an_admin_alert_is_never_quiet(tmp_path: Path) -> None:
    notifier = FlakyNotifier()
    async with open_bare(
        tmp_path / "alert.db", None, notifier=notifier, now=utc(2026, 10, 21, 21)
    ) as bare:
        install(PipelineJobs(bare.ctx, bare.system.delivery))
        await execute_job(JOB_REFRESH)  # no provider is configured: the job fails

    assert notifier.count("alert_admin") == 1
