"""The weekly learning job is registered, scheduled for Monday morning and routed."""

from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.pipeline.context import PipelineContext
from app.pipeline.delivery import NullDelivery
from app.pipeline.ledger import Ledger
from app.scheduler import JOB_LEARNING, JOB_NAMES, PipelineJobs, PipelineScheduler

TASHKENT = ZoneInfo("Asia/Tashkent")


async def test_learning_runs_on_monday_at_nine_local_time(tmp_path: Path) -> None:
    scheduler = PipelineScheduler(
        PipelineJobs.__new__(PipelineJobs), db_path=tmp_path / "jobs.db", timezone=TASHKENT
    )
    scheduler.start()
    try:
        assert JOB_LEARNING in JOB_NAMES
        next_run = scheduler.next_runs()[JOB_LEARNING]
        assert next_run is not None
        local = next_run.astimezone(TASHKENT)
        assert (local.weekday(), local.hour, local.minute) == (0, 9, 0)
    finally:
        await scheduler.stop()


async def test_the_learning_job_name_runs_the_weekly_review(
    sessions: async_sessionmaker[AsyncSession],
    ledger: Ledger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[PipelineContext] = []

    async def fake_review(ctx: PipelineContext) -> None:
        calls.append(ctx)

    monkeypatch.setattr("app.scheduler.run_learning", fake_review)
    ctx = PipelineContext(sessions=sessions, ledger=ledger)

    await PipelineJobs(ctx, NullDelivery()).run(JOB_LEARNING)

    assert calls == [ctx]
