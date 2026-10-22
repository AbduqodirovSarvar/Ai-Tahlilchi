"""The learning mode (off | shadow | apply) and the retro rule of the analysis hook (ADR 0013)."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.repo import list_learning_changes, list_segment_rows
from app.pipeline.context import PipelineContext
from app.pipeline.learning import KIND_SEGMENT_DISABLED, run_learning
from app.pipeline.learning_settings import LearningMode, LearningSettings
from app.pipeline.ledger import Ledger
from app.pipeline.overlay import DbLearningSource, NoLearning
from app.pipeline.runtime import open_runtime
from tests.pipeline.test_learning import Clock, context, next_week, seed_segment_history


def in_mode(ctx: PipelineContext, mode: LearningMode) -> PipelineContext:
    return replace(ctx, learning_settings=LearningSettings(mode=mode))


async def test_off_does_nothing_and_does_not_use_up_the_week(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    last = await seed_segment_history(sessions, n=120, hit_rate=0.30, clv_mean=-0.08)
    clock = Clock(last + timedelta(days=1))
    base = context(sessions, ledger, clock)

    report = await run_learning(in_mode(base, LearningMode.OFF))

    assert report.skipped
    async with sessions() as session:
        assert await list_segment_rows(session) == []
        assert await list_learning_changes(session) == []
    switched_on = await run_learning(in_mode(base, LearningMode.SHADOW))  # same week
    assert not switched_on.skipped


async def test_shadow_journals_what_it_would_do_flagged_as_not_applied(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    last = await seed_segment_history(sessions, n=120, hit_rate=0.30, clv_mean=-0.08)
    clock = Clock(last + timedelta(days=1))
    ctx = in_mode(context(sessions, ledger, clock), LearningMode.SHADOW)

    await run_learning(ctx)
    report = await next_week(ctx, clock)

    assert [c.kind for c in report.changes] == [KIND_SEGMENT_DISABLED]
    assert [c.applied for c in report.changes] == [False]
    async with sessions() as session:
        assert len(await list_learning_changes(session, applied=False)) == 1
        assert await list_learning_changes(session, applied=True) == []
        assert len(await list_learning_changes(session)) == 1  # no filter: both kinds


async def test_apply_journals_real_changes(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    last = await seed_segment_history(sessions, n=120, hit_rate=0.30, clv_mean=-0.08)
    clock = Clock(last + timedelta(days=1))
    ctx = in_mode(context(sessions, ledger, clock), LearningMode.APPLY)

    await run_learning(ctx)
    report = await next_week(ctx, clock)

    assert [c.applied for c in report.changes] == [True]
    async with sessions() as session:
        assert await list_learning_changes(session, applied=False) == []


async def test_the_runtime_reads_learning_only_in_apply_mode(tmp_path: Path) -> None:
    def settings(mode: str) -> Settings:
        return Settings(  # type: ignore[call-arg]
            _env_file=None,
            db_path=tmp_path / f"{mode}.db",
            learning_mode=mode,
            learning_manual_hold_days=14,
        )

    async with open_runtime(settings("shadow")) as runtime:
        assert isinstance(runtime.ctx.learning, NoLearning)
        assert runtime.ctx.learning_settings.mode is LearningMode.SHADOW
        assert runtime.ctx.learning_settings.manual_hold == timedelta(days=14)
    async with open_runtime(settings("off")) as runtime:
        assert isinstance(runtime.ctx.learning, NoLearning)
    async with open_runtime(settings("apply")) as runtime:
        assert isinstance(runtime.ctx.learning, DbLearningSource)


def test_shadow_is_the_default_and_a_bad_mode_is_refused() -> None:
    assert Settings(_env_file=None).learning_mode == "shadow"  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="learning_mode"):
        Settings(_env_file=None, learning_mode="yolo")  # type: ignore[call-arg]
