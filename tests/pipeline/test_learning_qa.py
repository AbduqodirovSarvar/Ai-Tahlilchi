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
from app.pipeline.learning import (
    KIND_CALIBRATION_ACTIVATED,
    rollback_learning_change,
    run_learning,
    week_start,
)
from app.pipeline.ledger import Ledger
from app.pipeline.overlay import EMPTY_OVERLAY, DbLearningSource
from app.scheduler import JOB_LEARNING, JOB_NAMES, PipelineJobs, PipelineScheduler
from tests.pipeline.test_learning import (
    MARKETS,
    START,
    Clock,
    Pending,
    add_settled,
    context,
    seed_fixtures,
    seed_probability_history,
    seed_segment_history,
)

TASHKENT = ZoneInfo("Asia/Tashkent")


async def seed_batch(
    sessions: async_sessionmaker[AsyncSession], *, league: str, fixtures: int, shift: float
) -> None:
    """A second batch of ~11 predictions per fixture in another league (no key collisions)."""
    rng = random.Random(77)  # noqa: S311 - simulation
    async with sessions.begin() as session:
        ids = await seed_fixtures(session, fixtures, league)
        pending = Pending([])
        for index, fixture_id in enumerate(ids):
            for pick in MARKETS:
                p = rng.uniform(0.45, 0.75)
                add_settled(
                    pending,
                    fixture_id,
                    hit=rng.random() < p - shift,
                    settled_at=START + timedelta(days=index, hours=9),
                    probability=p,
                    pick=pick,
                    clv=None,
                )
        await pending.flush(session)


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


# Findings pinned as strict xfail tests


@pytest.mark.slow
@pytest.mark.xfail(
    strict=True,
    reason="T5-QA-4: pushes (VOID) are dropped, so P(win) is compared with P(win | no push)",
)
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
    ctx = context(sessions, ledger, Clock(START + timedelta(days=2401)))

    report = await run_learning(ctx)

    assert not report.calibration_activated


@pytest.mark.slow
@pytest.mark.xfail(
    strict=True,
    reason="T5-QA-5: a manual calibration rollback is undone by the next weekly run",
)
async def test_a_manual_calibration_rollback_survives_the_next_weekly_run(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    last = await seed_probability_history(sessions, fixtures=400, shift=0.08)
    clock = Clock(last + timedelta(days=1))
    ctx = context(sessions, ledger, clock)
    change_id = await activation_of((await run_learning(ctx)).changes)
    assert await rollback_learning_change(ctx, change_id)
    await seed_batch(sessions, league="E9", fixtures=100, shift=0.08)

    clock.now += timedelta(weeks=1)
    await run_learning(ctx)

    # segments are frozen for MANUAL_HOLD after a rollback; the calibration must be, too
    async with sessions() as session:
        assert await active_calibration(session) is None


@pytest.mark.slow
@pytest.mark.xfail(
    strict=True,
    reason="T5-QA-6: rolling back an old activation switches off the newer active calibration",
)
async def test_rolling_back_an_old_activation_does_not_switch_off_a_newer_one(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    last = await seed_probability_history(sessions, fixtures=400, shift=0.08)
    clock = Clock(last + timedelta(days=1))
    ctx = context(sessions, ledger, clock)
    first = await activation_of((await run_learning(ctx)).changes)
    await seed_batch(sessions, league="E9", fixtures=200, shift=0.08)
    clock.now += timedelta(weeks=1)
    second = await run_learning(ctx)
    assert second.calibration_activated  # precondition: version 2 is now the active one
    async with sessions() as session:
        newest = await active_calibration(session)
    assert newest is not None
    assert newest.version == 2

    await rollback_learning_change(ctx, first)

    async with sessions() as session:
        assert await active_calibration(session) is not None


@pytest.mark.xfail(
    strict=True,
    raises=AttributeError,
    reason="T5-QA-7: a non-dict params value escapes the overlay fallback and breaks analysis",
)
async def test_a_calibration_row_with_non_dict_params_falls_back_to_the_raw_model(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions.begin() as session:
        session.add(
            CalibrationVersionRow(
                version=1,
                method="isotonic",
                params=[1, 2],  # type: ignore[arg-type]
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


# Job robustness


async def test_two_simultaneous_runs_in_one_week_do_exactly_one_review(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    last = await seed_segment_history(sessions, n=120, hit_rate=0.30, clv_mean=-0.08)
    ctx = context(sessions, ledger, Clock(last + timedelta(days=1)))

    first, second = await asyncio.gather(run_learning(ctx), run_learning(ctx))

    assert sorted([first.skipped, second.skipped]) == [False, True]
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
    assert len(retry.changes) == 1


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
