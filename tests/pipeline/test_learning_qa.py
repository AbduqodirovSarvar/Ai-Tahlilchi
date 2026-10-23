"""Independent QA of the learning loop integration (T5): job, rollback, overlay, scheduler."""

import asyncio
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.pipeline.learning as learning_module
from app.db.enums import Outcome
from app.db.repo import (
    active_calibration,
    list_calibration_versions,
    list_learning_changes,
    list_segment_rows,
    settled_learning_signals,
)
from app.db.schema import CalibrationVersionRow
from app.domain import Market, Selection
from app.models.calibration import CalibrationReason
from app.pipeline.learning import (
    KIND_CALIBRATION_ACTIVATED,
    KIND_SEGMENT_RELAXED,
    RollbackResult,
    can_push,
    rollback_change,
    rollback_learning_change,
    run_learning,
    week_start,
)
from app.pipeline.ledger import Ledger
from app.pipeline.overlay import EMPTY_OVERLAY, DbLearningSource, LearningOverlay, load_overlay
from app.scheduler import JOB_LEARNING, JOB_NAMES, PipelineJobs, PipelineScheduler
from tests.pipeline.test_learning import (
    START,
    Clock,
    Pending,
    activate_calibration,
    add_settled,
    context,
    next_week,
    seed_batch,
    seed_fixtures,
    seed_probability_history,
    seed_segment_history,
)

TASHKENT = ZoneInfo("Asia/Tashkent")


async def activation_of(report_changes: tuple[object, ...]) -> int:
    (change,) = [c for c in report_changes if getattr(c, "kind", "") == KIND_CALIBRATION_ACTIVATED]
    return int(change.id)  # type: ignore[attr-defined]


# Data selection


async def test_learning_uses_the_recommended_odds_not_the_current_ones(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions.begin() as session:
        ids = await seed_fixtures(session, 2)
        pending = Pending([])
        add_settled(pending, ids[0], hit=True, settled_at=START + timedelta(days=1), odds=1.5)
        pending.pairs[-1][0].recommended_odds = 2.5  # the price moved after the first call
        add_settled(pending, ids[1], hit=True, settled_at=START + timedelta(days=1), odds=1.9)
        pending.pairs[-1][0].recommended_odds = None  # legacy row without the field
        await pending.flush(session)

    async with sessions() as session:
        found = await settled_learning_signals(session, settled_before=START + timedelta(days=9))

    assert sorted(s.odds for s in found) == [1.9, 2.5]


# Findings of the QA round, now fixed (see docs/qa/T5-learning.md and ADR 0013 addendum)


@pytest.mark.slow
async def test_a_perfect_model_on_a_push_market_is_not_recalibrated(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    """Total 2.0: P(over)=0.4, P(push)=0.2, P(under)=0.4 -- the model is exactly right."""
    rng = random.Random(5)  # noqa: S311 - simulation
    async with sessions.begin() as session:
        ids = await seed_fixtures(session, 2400)
        pending = Pending([])
        for index, fixture_id in enumerate(ids):
            roll = rng.random()
            outcome = Outcome.WIN if roll < 0.4 else Outcome.VOID if roll < 0.6 else Outcome.LOSS
            add_settled(
                pending,
                fixture_id,
                hit=outcome is Outcome.WIN,
                outcome=outcome,
                settled_at=START + timedelta(days=index, hours=8),
                probability=0.4,
                pick=(Market.totals(2.0), Selection.OVER),
                clv=None,
            )
        await pending.flush(session)
    clock = Clock(START + timedelta(days=2401))
    ctx = context(sessions, ledger, clock)

    first = await run_learning(ctx)
    second = await next_week(ctx, clock)

    # a whole line can push: the markets are left out of the calibration data altogether
    assert first.calibration_reason is CalibrationReason.TOO_FEW_SAMPLES
    assert not second.calibration_activated
    async with sessions() as session:
        assert await list_calibration_versions(session) == []


async def test_half_lines_cannot_push_and_stay_in_the_calibration_data() -> None:
    assert can_push(Market.totals(2.0))
    assert can_push(Market.asian_handicap(0.0))
    assert can_push(Market.asian_handicap(-1.0))
    assert not can_push(Market.totals(2.5))
    assert not can_push(Market.totals(2.25))  # a quarter line splits the stake: no refund
    assert not can_push(Market.asian_handicap(-0.75))
    assert not can_push(Market.match_result())


@pytest.mark.slow
async def test_a_manual_calibration_rollback_survives_the_next_weekly_run(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    last = await seed_probability_history(sessions, fixtures=400, shift=0.15)
    clock = Clock(last + timedelta(days=1))
    ctx = context(sessions, ledger, clock)
    change_id = await activation_of((await activate_calibration(sessions, ctx, clock)).changes)
    assert await rollback_learning_change(ctx, change_id)
    await seed_batch(sessions, league="E8", fixtures=200, shift=0.15)

    report = await next_week(ctx, clock)

    # segments are frozen after a rollback; the calibration is, too
    assert report.calibration_version is not None  # the new fit is stored as evidence...
    assert not report.calibration_activated  # ...but not switched on
    async with sessions() as session:
        assert await active_calibration(session) is None


@pytest.mark.slow
async def test_rolling_back_an_old_activation_does_not_switch_off_a_newer_one(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    last = await seed_probability_history(sessions, fixtures=400, shift=0.15)
    clock = Clock(last + timedelta(days=1))
    ctx = context(sessions, ledger, clock)
    first = await activation_of((await activate_calibration(sessions, ctx, clock)).changes)
    await seed_batch(sessions, league="E8", fixtures=200, shift=0.15)
    second = await next_week(ctx, clock)
    assert second.calibration_activated  # precondition: a newer version is now the active one
    async with sessions() as session:
        newest = await active_calibration(session)
    assert newest is not None
    newer_version = newest.version

    result = await rollback_change(ctx, first)

    assert result is RollbackResult.STALE
    async with sessions() as session:
        still = await active_calibration(session)
        assert still is not None
        assert still.version == newer_version
        changes = await list_learning_changes(session)
    assert not next(c for c in changes if c.id == first).rolled_back  # refused, not half done


@pytest.mark.parametrize("params", [[1, 2], "knots", 7, None])
async def test_a_calibration_row_with_non_dict_params_falls_back_to_the_raw_model(
    sessions: async_sessionmaker[AsyncSession], params: object
) -> None:
    async with sessions.begin() as session:
        session.add(
            CalibrationVersionRow(
                version=1,
                method="isotonic",
                params=params,  # type: ignore[arg-type]
                n_samples=300,
                brier_before=0.25,
                brier_after=0.24,
                active=True,
                passed_gate=True,
                created_at=START,
            )
        )

    overlay = await DbLearningSource(sessions).load()

    assert overlay == EMPTY_OVERLAY


async def test_whatever_the_source_raises_the_analysis_gets_the_raw_model() -> None:
    class Broken:
        async def load(self) -> LearningOverlay:
            message = "boom"
            raise AttributeError(message)

    assert await load_overlay(Broken()) == EMPTY_OVERLAY


# Job robustness


async def test_two_simultaneous_runs_in_one_week_do_exactly_one_review(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    last = await seed_segment_history(sessions, n=120, hit_rate=0.30, clv_mean=-0.08)
    clock = Clock(last + timedelta(days=1))
    ctx = context(sessions, ledger, clock)

    first, second = await asyncio.gather(run_learning(ctx), run_learning(ctx))

    assert sorted([first.skipped, second.skipped]) == [False, True]
    async with sessions() as session:
        assert [r.flagged for r in await list_segment_rows(session)] == [True]
        assert await list_learning_changes(session) == []  # one look so far: only a flag
    await next_week(ctx, clock)
    async with sessions() as session:
        assert len(await list_learning_changes(session)) == 1


async def test_a_crash_after_the_segments_leaves_no_partial_week_and_can_be_retried(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    last = await seed_segment_history(sessions, n=120, hit_rate=0.30, clv_mean=-0.08)
    clock = Clock(last + timedelta(days=1))
    ctx = context(sessions, ledger, clock)

    async def broken(*_args: object, **_kwargs: object) -> object:
        message = "killed between segments and calibration"
        raise OSError(message)

    monkeypatch.setattr(learning_module, "_apply_calibration", broken)
    with pytest.raises(OSError, match="killed"):
        await run_learning(ctx)
    async with sessions() as session:
        assert await list_segment_rows(session) == []  # the segment writes were rolled back
        assert await list_learning_changes(session) == []
        assert await list_calibration_versions(session) == []
    monkeypatch.undo()

    clock.now += timedelta(hours=6)  # past the retry backoff, still the same ISO week
    retry = await run_learning(ctx)

    assert not retry.skipped
    assert retry.changes == ()  # the first confirmed look only flags the segment
    assert len((await next_week(ctx, clock)).changes) == 1


def test_the_scheduled_fire_time_maps_to_its_own_week_for_a_year() -> None:
    first_fire = datetime(2026, 1, 5, 9, tzinfo=TASHKENT).astimezone(UTC)  # Monday 09:00 local
    keys = [week_start(first_fire + timedelta(weeks=n)) for n in range(52)]
    assert len(set(keys)) == 52
    assert all(key.weekday() == 0 for key in keys)


async def test_the_learning_job_is_registered_once_across_restarts(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.db"
    for _ in range(3):  # three process lifetimes over the same persisted job store
        scheduler = PipelineScheduler(
            PipelineJobs.__new__(PipelineJobs), db_path=db_path, timezone=TASHKENT
        )
        scheduler.start()
        try:
            runs = scheduler.next_runs()
            assert sorted(runs) == sorted(JOB_NAMES)  # one entry per job id, learning included
            fire = runs[JOB_LEARNING]
            assert fire is not None
            local = fire.astimezone(TASHKENT)
            assert (local.weekday(), local.hour, local.minute) == (0, 9, 0)
        finally:
            await scheduler.stop()


@pytest.mark.slow
async def test_an_active_calibration_is_not_replaced_without_clearly_more_data(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    last = await seed_probability_history(sessions, fixtures=400, shift=0.15)
    clock = Clock(last + timedelta(days=1))
    ctx = context(sessions, ledger, clock)
    await activate_calibration(sessions, ctx, clock)
    async with sessions() as session:
        active = await active_calibration(session)
    assert active is not None
    await seed_batch(sessions, league="E8", fixtures=30, shift=0.15)  # +6% data only

    report = await next_week(ctx, clock)

    assert report.calibration_version == active.version + 1  # stored as evidence...
    assert not report.calibration_activated  # ...but the active map stays
    async with sessions() as session:
        still = await active_calibration(session)
    assert still is not None
    assert still.version == active.version


async def test_a_segment_change_that_was_superseded_cannot_be_rolled_back(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    last = await seed_segment_history(sessions, n=120, hit_rate=0.30, clv_mean=-0.08)
    clock = Clock(last + timedelta(days=1))
    ctx = context(sessions, ledger, clock)
    await run_learning(ctx)
    (disabled,) = (await next_week(ctx, clock)).changes
    clock.now += timedelta(days=61)  # the cooldown is over: the segment returns on probation
    (relaxed,) = (await run_learning(ctx)).changes
    assert relaxed.kind == KIND_SEGMENT_RELAXED

    result = await rollback_change(ctx, disabled.id)

    assert result is RollbackResult.STALE
    async with sessions() as session:
        (row,) = await list_segment_rows(session)
        assert row.enabled
        assert row.ev_threshold > 0.0  # still the probation the later change put it in
        assert row.hold_until is None  # no hold was set: nothing was rolled back
    assert await rollback_change(ctx, 9999) is RollbackResult.NOT_FOUND
    assert await rollback_change(ctx, relaxed.id) is RollbackResult.ROLLED_BACK
    assert await rollback_change(ctx, relaxed.id) is RollbackResult.ALREADY_ROLLED_BACK
