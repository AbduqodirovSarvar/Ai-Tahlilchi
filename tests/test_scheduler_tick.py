"""The scheduler's pulse for the watchdog: age since the last ``daily-tick`` (or start)."""

from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app import scheduler as scheduler_module
from app.scheduler import JOB_DAILY_TICK, JOB_SETTLE, PipelineJobs, PipelineScheduler, execute_job

TASHKENT = ZoneInfo("Asia/Tashkent")


class Recorder(PipelineJobs):
    def __init__(self) -> None:
        self.ran: list[str] = []

    async def run(self, name: str) -> None:
        self.ran.append(name)


def clock(monkeypatch: pytest.MonkeyPatch, now: float) -> None:
    monkeypatch.setattr(scheduler_module, "monotonic", lambda: now)


async def test_the_pulse_starts_at_start_and_is_renewed_by_each_tick(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    scheduler = PipelineScheduler(Recorder(), db_path=tmp_path / "jobs.db", timezone=TASHKENT)
    assert scheduler.seconds_since_tick() == 0.0  # not started: the watchdog must stay quiet

    clock(monkeypatch, 1000.0)
    scheduler.start()
    clock(monkeypatch, 1300.0)
    assert scheduler.seconds_since_tick() == 300.0

    await execute_job(JOB_SETTLE)  # other jobs are not the pulse
    assert scheduler.seconds_since_tick() == 300.0

    clock(monkeypatch, 1310.0)
    await execute_job(JOB_DAILY_TICK)
    clock(monkeypatch, 1320.0)
    assert scheduler.seconds_since_tick() == 10.0
    await scheduler.stop()


async def test_installing_new_jobs_resets_the_pulse(tmp_path: Path) -> None:
    await execute_job(JOB_DAILY_TICK)
    assert scheduler_module._last_tick_at is not None
    PipelineScheduler(Recorder(), db_path=tmp_path / "jobs.db", timezone=TASHKENT)
    assert scheduler_module._last_tick_at is None
