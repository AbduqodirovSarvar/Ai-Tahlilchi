"""T6 fix round A: shadow/off keep learned context out of the LLM; shadow -> apply is journaled."""

from dataclasses import replace
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.repo import active_calibration, list_learning_changes, list_segment_rows
from app.models.segments import MIN_SEGMENT_SAMPLE
from app.pipeline.context import PipelineContext
from app.pipeline.learning import (
    KIND_CALIBRATION_ACTIVATED,
    KIND_PROMOTED,
    KIND_SEGMENT_DISABLED,
    RollbackResult,
    promote_shadow_state,
    rollback_change,
    run_learning,
)
from app.pipeline.learning_settings import LearningMode, LearningSettings
from app.pipeline.ledger import Ledger
from app.pipeline.runtime import build_reviewer
from tests.pipeline.feedback_world import open_world, versus
from tests.pipeline.test_experience import RecordingService, seeded, segment_row
from tests.pipeline.test_learning import (
    Clock,
    activate_calibration,
    context,
    next_week,
    seed_probability_history,
    seed_segment_history,
)


def in_mode(ctx: PipelineContext, mode: LearningMode) -> PipelineContext:
    return replace(ctx, learning_settings=LearningSettings(mode=mode))


# ----------------------------------------------------------------------------- decision #20
async def test_learned_context_reaches_the_llm_only_in_apply_mode(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async with open_world(tmp_path / "modes.db", (versus("Chelsea", "Arsenal"), 1, 0)) as bare:
        league_id, ref, built = await seeded(bare)
        await segment_row(bare, league_id, ref, "1.50-1.80", MIN_SEGMENT_SAMPLE + 10, 0.10, 0.06)
        seen: dict[LearningMode, RecordingService] = {}
        for mode in LearningMode:
            service = RecordingService()
            reviewer = build_reviewer(service, bare.sessions, bare.clock, mode)  # type: ignore[arg-type]
            await reviewer.review([built])
            seen[mode] = service

    assert seen[LearningMode.OFF].contexts == [None]
    assert seen[LearningMode.SHADOW].contexts == [None]
    applied = seen[LearningMode.APPLY].contexts
    assert len(applied) == 1
    assert applied[0] is not None
    assert applied[0].segment_stats  # the learned statistics are there
    # the candidates the model sees are the same in every mode: only the context differs
    assert seen[LearningMode.OFF].inputs == seen[LearningMode.SHADOW].inputs
    assert seen[LearningMode.SHADOW].inputs == seen[LearningMode.APPLY].inputs


# ----------------------------------------------------------------------------- decision #23
async def _shadow_with_a_disabled_segment(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> tuple[PipelineContext, Clock]:
    last = await seed_segment_history(sessions, n=120, hit_rate=0.30, clv_mean=-0.08)
    clock = Clock(last + timedelta(days=1))
    ctx = in_mode(context(sessions, ledger, clock), LearningMode.SHADOW)
    await run_learning(ctx)
    await next_week(ctx, clock)
    return ctx, clock


async def test_switching_to_apply_journals_the_shadow_state_as_applied(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    ctx, _ = await _shadow_with_a_disabled_segment(sessions, ledger)
    async with sessions() as session:
        assert [c.applied for c in await list_learning_changes(session)] == [False]
        assert [r.enabled for r in await list_segment_rows(session)] == [False]

    applying = in_mode(ctx, LearningMode.APPLY)
    written = await promote_shadow_state(applying)

    assert written == 1
    async with sessions() as session:
        [promoted] = await list_learning_changes(session, applied=True)
    assert promoted.kind == KIND_SEGMENT_DISABLED
    assert promoted.reason == KIND_PROMOTED
    assert promoted.before["enabled"] is True  # undoing it restores the neutral state
    assert promoted.after["enabled"] is False
    assert await rollback_change(applying, promoted.id) is RollbackResult.ROLLED_BACK
    async with sessions() as session:
        assert [r.enabled for r in await list_segment_rows(session)] == [True]


async def test_the_promotion_runs_once_and_before_the_weekly_claim(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    ctx, _ = await _shadow_with_a_disabled_segment(sessions, ledger)
    applying = in_mode(ctx, LearningMode.APPLY)

    report = await run_learning(applying)  # the week is already taken: no weekly work
    again = await promote_shadow_state(applying)

    assert report.skipped
    assert again == 0
    async with sessions() as session:
        assert len(await list_learning_changes(session, applied=True)) == 1


async def test_a_shadow_run_after_apply_is_promoted_again_on_the_next_switch(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    ctx, clock = await _shadow_with_a_disabled_segment(sessions, ledger)
    applying = in_mode(ctx, LearningMode.APPLY)
    assert await promote_shadow_state(applying) == 1

    await next_week(in_mode(ctx, LearningMode.SHADOW), clock)  # back to shadow for a week
    clock.now += timedelta(seconds=1)

    assert await promote_shadow_state(applying) == 1  # state may have moved: journal it again


async def test_a_database_that_never_ran_shadow_has_nothing_to_promote(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    ctx = in_mode(context(sessions, ledger, Clock(seed_start())), LearningMode.APPLY)

    assert await promote_shadow_state(ctx) == 0
    assert await promote_shadow_state(ctx) == 0
    async with sessions() as session:
        assert await list_learning_changes(session) == []


def seed_start():  # type: ignore[no-untyped-def]
    from tests.pipeline.world import START  # noqa: PLC0415

    return START + timedelta(days=200)


async def test_a_shadow_calibration_is_promoted_and_can_be_undone(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    last = await seed_probability_history(sessions, fixtures=400, shift=0.15)
    clock = Clock(last + timedelta(days=1))
    shadow = in_mode(context(sessions, ledger, clock), LearningMode.SHADOW)
    report = await activate_calibration(sessions, shadow, clock)
    assert report.calibration_activated
    async with sessions() as session:
        stored = await active_calibration(session)
    assert stored is not None

    applying = in_mode(shadow, LearningMode.APPLY)
    written = await promote_shadow_state(applying)

    assert written >= 1
    async with sessions() as session:
        entries = list(await list_learning_changes(session, applied=True))
    [entry] = [c for c in entries if c.kind == KIND_CALIBRATION_ACTIVATED]
    assert entry.after["version"] == stored.version
    assert await rollback_change(applying, entry.id) is RollbackResult.ROLLED_BACK
    async with sessions() as session:
        assert await active_calibration(session) is None
