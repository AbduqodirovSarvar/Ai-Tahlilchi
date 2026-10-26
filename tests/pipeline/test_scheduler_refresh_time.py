"""The daily refresh runs at the configured local time and a restart picks up a new time."""

from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

from app.pipeline.config import DEFAULT_REFRESH_AT
from app.scheduler import JOB_REFRESH, PipelineJobs, PipelineScheduler

TASHKENT = ZoneInfo("Asia/Tashkent")


def _refresh_time(scheduler: PipelineScheduler) -> tuple[int, int]:
    next_run = scheduler.next_runs()[JOB_REFRESH]
    assert next_run is not None
    local = next_run.astimezone(TASHKENT)
    return local.hour, local.minute


async def test_the_refresh_defaults_to_half_past_eight(tmp_path: Path) -> None:
    scheduler = PipelineScheduler(
        PipelineJobs.__new__(PipelineJobs), db_path=tmp_path / "jobs.db", timezone=TASHKENT
    )
    scheduler.start()
    try:
        assert _refresh_time(scheduler) == (DEFAULT_REFRESH_AT.hour, DEFAULT_REFRESH_AT.minute)
    finally:
        await scheduler.stop()


async def test_a_configured_time_survives_restarts_and_a_change_reschedules(
    tmp_path: Path,
) -> None:
    path = tmp_path / "jobs.db"
    seen = []
    for refresh_at in (time(6, 15), time(6, 15), time(23, 45)):
        scheduler = PipelineScheduler(
            PipelineJobs.__new__(PipelineJobs),
            db_path=path,
            timezone=TASHKENT,
            refresh_at=refresh_at,
        )
        scheduler.start()
        try:
            seen.append(_refresh_time(scheduler))
            assert len(scheduler.next_runs()) == 9
        finally:
            await scheduler.stop()

    assert seen == [(6, 15), (6, 15), (23, 45)]
