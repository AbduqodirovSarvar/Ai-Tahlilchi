"""APScheduler wiring: jobs, persistence across restarts, failure reporting."""

from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app import scheduler as scheduler_module
from app.scheduler import (
    JOB_DAILY_TICK,
    JOB_NAMES,
    JOB_RECALC_TICK,
    JOB_REFRESH,
    JOB_SETTLE,
    PipelineJobs,
    PipelineScheduler,
    execute_job,
)
from tests.pipeline.harness import Env

TASHKENT = ZoneInfo("Asia/Tashkent")


async def test_jobs_are_registered_and_survive_a_restart(seeded_env: Env, tmp_path: Path) -> None:
    jobs = PipelineJobs(seeded_env.ctx, seeded_env.system.delivery)
    store = tmp_path / "jobs.db"

    first = PipelineScheduler(jobs, db_path=store, timezone=TASHKENT)
    first.start()
    before = first.next_runs()
    await first.stop()

    second = PipelineScheduler(jobs, db_path=store, timezone=TASHKENT)
    second.start()
    after = second.next_runs()
    await second.stop()

    assert set(before) == set(after) == set(JOB_NAMES)
    assert before[JOB_SETTLE] == after[JOB_SETTLE]
    assert before[JOB_REFRESH] == after[JOB_REFRESH]
    assert before[JOB_SETTLE].hour == 8
    assert before[JOB_SETTLE].minute == 0
    assert before[JOB_REFRESH].hour == 8
    assert before[JOB_REFRESH].minute == 30


async def test_stopping_twice_is_harmless(seeded_env: Env, tmp_path: Path) -> None:
    scheduler = PipelineScheduler(
        PipelineJobs(seeded_env.ctx, seeded_env.system.delivery),
        db_path=tmp_path / "jobs.db",
        timezone=TASHKENT,
    )
    scheduler.start()
    await scheduler.stop()
    await scheduler.stop()


@pytest.mark.parametrize("name", [JOB_SETTLE, JOB_DAILY_TICK, JOB_RECALC_TICK])
async def test_each_job_runs_on_the_installed_runtime(seeded_env: Env, name: str) -> None:
    scheduler_module.install(PipelineJobs(seeded_env.ctx, seeded_env.system.delivery))

    await execute_job(name)

    assert seeded_env.notifier.sent == []


async def test_a_failing_job_is_recorded_and_reported_to_the_admins(seeded_env: Env) -> None:
    scheduler_module.install(PipelineJobs(seeded_env.ctx, seeded_env.system.delivery))

    await execute_job(JOB_REFRESH)  # no provider is configured: the job fails

    runs = await seeded_env.ctx.ledger.last_runs()
    assert not runs[JOB_REFRESH].ok
    alerts = [s for s in seeded_env.notifier.sent if s.method == "alert_admin"]
    assert len(alerts) == 1
    assert alerts[0].payload.component == JOB_REFRESH  # type: ignore[attr-defined]
    assert [e[1] for e in await seeded_env.ctx.ledger.recent_errors()] == [JOB_REFRESH]


async def test_a_trigger_without_a_runtime_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler_module, "_installed", None)
    await execute_job(JOB_SETTLE)
