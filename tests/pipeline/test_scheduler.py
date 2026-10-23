"""APScheduler wiring: jobs, persistence across restarts, failure reporting."""

from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.exc import OperationalError

from app import scheduler as scheduler_module
from app.scheduler import (
    FAILURE_BACKOFF_SECONDS,
    JOB_DAILY_TICK,
    JOB_NAMES,
    JOB_RECALC_TICK,
    JOB_REFRESH,
    JOB_RETENTION,
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


async def test_retention_is_registered_weekly_on_sunday_at_four(
    seeded_env: Env, tmp_path: Path
) -> None:
    scheduler = PipelineScheduler(
        PipelineJobs(seeded_env.ctx, seeded_env.system.delivery),
        db_path=tmp_path / "jobs.db",
        timezone=TASHKENT,
    )
    scheduler.start()
    try:
        nxt = scheduler.next_runs()[JOB_RETENTION]
    finally:
        await scheduler.stop()

    assert nxt is not None
    local = nxt.astimezone(TASHKENT)
    assert (local.weekday(), local.hour, local.minute) == (6, 4, 0)


async def test_the_retention_job_runs_once_a_week_and_is_recorded_for_holat(
    seeded_env: Env,
) -> None:
    jobs = PipelineJobs(seeded_env.ctx, seeded_env.system.delivery)

    await jobs.run(JOB_RETENTION)
    first = (await seeded_env.ctx.ledger.last_runs())[JOB_RETENTION]
    await jobs.run(JOB_RETENTION)  # the same week: skipped, nothing re-recorded

    assert first.ok
    assert first.detail == "deleted=0"
    assert (await seeded_env.ctx.ledger.last_runs())[JOB_RETENTION] == first


async def test_a_failure_that_cannot_be_recorded_is_logged_and_backs_off_not_raised(
    seeded_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    async def locked(*_args: object, **_kwargs: object) -> None:
        raise OperationalError("UPDATE", {}, Exception("database is locked"))

    monkeypatch.setattr(seeded_env.ctx.ledger, "record_run", locked)
    monkeypatch.setattr(seeded_env.ctx.ledger, "record_error", locked)
    jobs = PipelineJobs(seeded_env.ctx, seeded_env.system.delivery, sleep=fake_sleep)

    await jobs.run(JOB_REFRESH)  # fails (no provider) and then cannot even record it

    assert slept == [FAILURE_BACKOFF_SECONDS]


async def test_one_users_unrecordable_failure_does_not_stop_the_other_users(
    seeded_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.pipeline import daily  # noqa: PLC0415

    served: list[int] = []

    async def serve(_ctx: object, _delivery: object, user_id: int) -> str:
        served.append(user_id)
        if user_id == served[0]:
            raise OperationalError("UPDATE", {}, Exception("database is locked"))
        return "sent"

    monkeypatch.setattr(daily, "_serve", serve)
    monkeypatch.setattr(daily, "user_ids", _two_users)

    report = await daily.run_due_digests(seeded_env.ctx, seeded_env.system.delivery)

    assert len(served) == 2
    assert report.failed == (served[0],)
    assert report.sent == (served[1],)


async def _two_users(_session: object) -> list[int]:
    return [101, 102]
