"""T4 QA: the scheduler across restarts, including a run missed while the process was down."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.scheduler import (
    JOB_DAILY_TICK,
    JOB_NAMES,
    JOB_RECALC_TICK,
    JOB_REFRESH,
    JOB_SETTLE,
    PipelineJobs,
    PipelineScheduler,
)

TASHKENT = ZoneInfo("Asia/Tashkent")
WAIT_SECONDS = 10.0  # generous: only a missing run waits this long
MISSING_SECONDS = 3.0


class Recorder(PipelineJobs):
    """Jobs that only remember that they ran."""

    def __init__(self) -> None:
        self.ran: list[str] = []

    async def run(self, name: str) -> None:
        self.ran.append(name)


async def wait_until(condition, seconds: float = WAIT_SECONDS) -> bool:  # type: ignore[no-untyped-def]
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.05)
    return False


async def stopped_with_overdue(store: Path, name: str, overdue: timedelta) -> None:
    """Persist the schedule, then make job ``name`` overdue as if the process had been down."""
    first = PipelineScheduler(Recorder(), db_path=store, timezone=TASHKENT)
    first.start()
    first._scheduler.pause()
    job = first._scheduler.get_job(name)
    assert job is not None
    job.modify(next_run_time=datetime.now(UTC) - overdue)
    await first.stop()


async def test_a_restart_never_duplicates_or_loses_a_job(tmp_path: Path) -> None:
    store = tmp_path / "jobs.db"
    for _ in range(3):
        scheduler = PipelineScheduler(Recorder(), db_path=store, timezone=TASHKENT)
        scheduler.start()
        assert sorted(scheduler.next_runs()) == sorted(JOB_NAMES)
        await scheduler.stop()


@pytest.mark.slow
@pytest.mark.parametrize("name", [JOB_SETTLE, JOB_REFRESH])
async def test_a_daily_job_missed_while_down_runs_after_the_restart(
    tmp_path: Path, name: str
) -> None:
    store = tmp_path / "jobs.db"
    await stopped_with_overdue(store, name, timedelta(hours=1))
    jobs = Recorder()
    second = PipelineScheduler(jobs, db_path=store, timezone=TASHKENT)
    second.start()
    try:
        ran = await wait_until(lambda: name in jobs.ran, MISSING_SECONDS)
    finally:
        await second.stop()

    assert ran


@pytest.mark.slow
async def test_the_overdue_job_is_kept_when_it_is_registered_without_replacing(
    tmp_path: Path,
) -> None:
    """Control for the finding: APScheduler itself *would* run a persisted overdue job."""
    store = tmp_path / "jobs.db"
    await stopped_with_overdue(store, JOB_SETTLE, timedelta(hours=1))
    jobs = Recorder()
    second = PipelineScheduler(jobs, db_path=store, timezone=TASHKENT)
    second._scheduler.start()  # no re-registration: the stored (overdue) job is loaded as it was
    try:
        ran = await wait_until(lambda: JOB_SETTLE in jobs.ran)
    finally:
        await second.stop()

    assert ran


async def test_the_ticks_are_every_minute_and_the_daily_jobs_use_the_display_zone(
    tmp_path: Path,
) -> None:
    scheduler = PipelineScheduler(Recorder(), db_path=tmp_path / "jobs.db", timezone=TASHKENT)
    scheduler.start()
    try:
        runs = scheduler.next_runs()
    finally:
        await scheduler.stop()

    now = datetime.now(UTC)
    for tick in (JOB_DAILY_TICK, JOB_RECALC_TICK):
        next_run = runs[tick]
        assert next_run is not None
        assert timedelta(0) < next_run - now <= timedelta(seconds=61)
    settle, refresh = runs[JOB_SETTLE], runs[JOB_REFRESH]
    assert settle is not None
    assert refresh is not None
    assert settle.astimezone(TASHKENT).time().replace(tzinfo=None).isoformat() == "08:00:00"
    assert refresh.astimezone(TASHKENT).time().replace(tzinfo=None).isoformat() == "08:30:00"
