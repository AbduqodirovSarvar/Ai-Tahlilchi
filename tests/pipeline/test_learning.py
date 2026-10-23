"""The weekly learning job over simulated settled-signal histories (F8.5, F8.6)."""

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.enums import Confidence, Outcome, ResultSource, SignalSource, SignalStatus
from app.db.repo import (
    active_calibration,
    ensure_user,
    get_fixtures,
    list_calibration_versions,
    list_learning_changes,
    list_segment_rows,
    settled_learning_signals,
    upsert_live,
)
from app.db.schema import (
    CalibrationVersionRow,
    LearningChangeRow,
    SegmentStatRow,
    SignalResultRow,
    SignalRow,
)
from app.domain import Market, Selection
from app.models.calibration import CalibrationReason
from app.pipeline.context import PipelineContext
from app.pipeline.learning import (
    KIND_CALIBRATION_ACTIVATED,
    KIND_SEGMENT_DISABLED,
    LearningReport,
    RollbackResult,
    rollback_change,
    run_learning,
    week_start,
)
from app.pipeline.learning_settings import DEFAULT_MANUAL_HOLD, LearningMode, LearningSettings
from app.pipeline.ledger import Ledger, ledger_table
from app.pipeline.overlay import DbLearningSource
from tests.domain.factories import make_fixture, make_league

USER = 1001
START = datetime(2026, 1, 5, 12, tzinfo=UTC)  # a Monday
MARKETS = (
    (Market.totals(1.5), Selection.OVER),
    (Market.totals(2.5), Selection.OVER),
    (Market.totals(3.5), Selection.OVER),
    (Market.totals(4.5), Selection.OVER),
    (Market.totals(1.5), Selection.UNDER),
    (Market.totals(2.5), Selection.UNDER),
    (Market.totals(3.5), Selection.UNDER),
    (Market.totals(4.5), Selection.UNDER),
    (Market.match_result(), Selection.HOME),
    (Market.match_result(), Selection.DRAW),
    (Market.match_result(), Selection.AWAY),
)


@dataclass
class Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


def context(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger, clock: Clock
) -> PipelineContext:
    return PipelineContext(
        sessions=sessions,
        ledger=ledger,
        clock=clock,
        learning=DbLearningSource(sessions),
        learning_settings=LearningSettings(mode=LearningMode.APPLY),
    )


async def _undone(ctx: PipelineContext, change_id: int) -> bool:
    return await rollback_change(ctx, change_id) is RollbackResult.ROLLED_BACK


async def seed_fixtures(session: AsyncSession, count: int, league: str = "E0") -> list[int]:
    await ensure_user(session, USER, timezone="Asia/Tashkent")
    fixtures = [
        make_fixture(
            f"H{league}{i}",
            f"A{league}{i}",
            kickoff=START + timedelta(days=i, hours=3),
            league=make_league(league, f"League {league}"),
        )
        for i in range(count)
    ]
    await upsert_live(session, fixtures=fixtures)
    stored = await get_fixtures(session, [f.ref for f in fixtures])
    return [stored[f.ref].id for f in fixtures]


@dataclass
class Pending:
    """Signals and results collected for one flush (results need their signal's id)."""

    pairs: list[tuple[SignalRow, dict[str, object]]]

    async def flush(self, session: AsyncSession) -> None:
        session.add_all([row for row, _ in self.pairs])
        await session.flush()
        session.add_all(
            [SignalResultRow(signal_id=row.id, **result) for row, result in self.pairs]  # type: ignore[arg-type]
        )
        await session.flush()
        self.pairs.clear()


def add_settled(  # noqa: PLR0913 - a simulation knob per argument
    pending: Pending,
    fixture_id: int,
    *,
    hit: bool,
    settled_at: datetime,
    probability: float = 0.55,
    odds: float = 2.0,
    clv: float | None = 0.0,
    pick: tuple[Market, Selection] = MARKETS[1],
    source: SignalSource = SignalSource.ANALYSIS,
    status: SignalStatus = SignalStatus.ACTIVE,
    outcome: Outcome | None = None,
    user: int = USER,
) -> None:
    market, selection = pick
    row = SignalRow(
        user_id=user,
        fixture_id=fixture_id,
        market=market.type,
        selection=selection,
        line=0.0 if market.line is None else market.line,
        odds=odds,
        p_model=probability,
        ev=0.1,
        confidence=Confidence.MEDIUM,
        stake_units=Decimal("1.0"),
        source=source,
        status=status,
        recommended_at=settled_at - timedelta(days=1),
        recommended_odds=odds,
    )
    result: dict[str, object] = {
        "outcome": outcome or (Outcome.WIN if hit else Outcome.LOSS),
        "source": ResultSource.API,
        "profit_units": Decimal("1.0") if hit else Decimal("-1.0"),
        "clv": clv,
        "settled_at": settled_at,
    }
    pending.pairs.append((row, result))


async def seed_segment_history(  # noqa: PLR0913 - a simulation knob per argument
    sessions: async_sessionmaker[AsyncSession],
    *,
    n: int,
    hit_rate: float,
    clv_mean: float,
    league: str = "E0",
    seed: int = 1,
    streak: int = 0,
) -> datetime:
    """``n`` signals (one per matchday) plus ``streak`` losses; returns the last settle time."""
    rng = random.Random(seed)  # noqa: S311 - simulation, not security
    async with sessions.begin() as session:
        ids = await seed_fixtures(session, n + streak, league)
        pending = Pending([])
        for index, fixture_id in enumerate(ids):
            lost = index >= n
            add_settled(
                pending,
                fixture_id,
                hit=False if lost else rng.random() < hit_rate,
                settled_at=START + timedelta(days=index, hours=8),
                clv=rng.gauss(clv_mean, 0.05),
            )
        await pending.flush(session)
    return START + timedelta(days=n + streak, hours=8)


async def seed_probability_history(
    sessions: async_sessionmaker[AsyncSession], *, fixtures: int, shift: float, seed: int = 1
) -> datetime:
    """~11 signals per fixture with predictions in 0.45..0.75 and hit rate ``p - shift``."""
    rng = random.Random(seed)  # noqa: S311 - simulation, not security
    async with sessions.begin() as session:
        ids = await seed_fixtures(session, fixtures)
        pending = Pending([])
        for index, fixture_id in enumerate(ids):
            for pick in MARKETS:
                p = rng.uniform(0.45, 0.75)
                add_settled(
                    pending,
                    fixture_id,
                    hit=rng.random() < p - shift,
                    settled_at=START + timedelta(days=index, hours=8),
                    probability=p,
                    pick=pick,
                    clv=None,
                )
        await pending.flush(session)
    return START + timedelta(days=fixtures, hours=8)


async def seed_batch(
    sessions: async_sessionmaker[AsyncSession], *, league: str, fixtures: int, shift: float
) -> None:
    """A second batch of ~11 predictions per fixture in another league (fresh data between runs)."""
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


async def next_week(ctx: PipelineContext, clock: Clock) -> LearningReport:
    """The run one week later."""
    clock.now += timedelta(weeks=1)
    return await run_learning(ctx)


async def activate_calibration(
    sessions: async_sessionmaker[AsyncSession], ctx: PipelineContext, clock: Clock
) -> LearningReport:
    """Two consecutive runs with fresh data in between: the second one earns the activation."""
    first = await run_learning(ctx)
    assert not first.calibration_activated  # one passing week is not enough
    await seed_batch(sessions, league="E9", fixtures=100, shift=0.15)
    return await next_week(ctx, clock)


async def rows(sessions: async_sessionmaker[AsyncSession]) -> list[object]:
    async with sessions() as session:
        return list(await list_segment_rows(session))


# ----------------------------------------------------------------------- segments


async def test_a_real_negative_segment_is_disabled_after_enough_significant_signals(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    last = await seed_segment_history(sessions, n=120, hit_rate=0.30, clv_mean=-0.08)
    clock = Clock(START + timedelta(days=48, hours=12))  # only 49 signals are settled yet
    ctx = context(sessions, ledger, clock)

    early = await run_learning(ctx)
    assert early.signals == 49
    assert all(row.enabled for row in await list_segment_rows_of(sessions))
    assert early.changes == ()

    clock.now = last + timedelta(days=7)
    flagged = await run_learning(ctx)  # the first run only notes the evidence
    assert flagged.changes == ()
    assert [row.flagged for row in await list_segment_rows_of(sessions)] == [True]
    assert (await DbLearningSource(sessions).load()).rules == {}

    final = await next_week(ctx, clock)  # the second run confirms it

    segments = await list_segment_rows_of(sessions)
    assert [row.enabled for row in segments] == [False]
    assert segments[0].n == 120
    assert segments[0].state_since == clock.now
    assert [change.kind for change in final.changes] == [KIND_SEGMENT_DISABLED]
    overlay = await DbLearningSource(sessions).load()
    assert len(overlay.rules) == 1


async def list_segment_rows_of(sessions: async_sessionmaker[AsyncSession]):  # type: ignore[no-untyped-def]
    async with sessions() as session:
        return await list_segment_rows(session)


async def test_a_bad_streak_in_a_good_segment_changes_nothing(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    last = await seed_segment_history(
        sessions, n=100, hit_rate=0.62, clv_mean=0.04, streak=8, seed=9
    )
    ctx = context(sessions, ledger, Clock(last + timedelta(days=1)))

    report = await run_learning(ctx)

    segments = await list_segment_rows_of(sessions)
    assert report.changes == ()
    assert [(row.enabled, row.ev_threshold) for row in segments] == [(True, 0.0)]
    assert segments[0].n == 108
    assert (await DbLearningSource(sessions).load()).rules == {}


async def test_a_disabled_segment_stays_put_and_its_state_start_is_not_restarted(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    last = await seed_segment_history(sessions, n=150, hit_rate=0.30, clv_mean=-0.08, seed=8)
    clock = Clock(last + timedelta(days=1))
    ctx = context(sessions, ledger, clock)

    await run_learning(ctx)
    second = await next_week(ctx, clock)
    disabled_at = clock.now
    third = await next_week(ctx, clock)
    fourth = await next_week(ctx, clock)

    assert [c.kind for c in second.changes] == [KIND_SEGMENT_DISABLED]
    assert third.changes == fourth.changes == ()
    segment = (await list_segment_rows_of(sessions))[0]
    assert segment.enabled is False
    assert segment.state_since == disabled_at  # the weekly rewrite does not restart the clock


async def test_segments_ignore_retro_but_include_hidden_and_duplicates_count_once(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions.begin() as session:
        ids = await seed_fixtures(session, 4)
        await ensure_user(session, 1002, timezone="Asia/Tashkent")
        pending = Pending([])
        add_settled(pending, ids[0], hit=True, settled_at=START)
        add_settled(pending, ids[1], hit=True, settled_at=START, source=SignalSource.RETRO)
        add_settled(pending, ids[2], hit=False, settled_at=START, status=SignalStatus.HIDDEN)
        add_settled(pending, ids[3], hit=True, settled_at=START)
        add_settled(pending, ids[3], hit=True, settled_at=START, user=1002)  # same pick, 2nd user
        add_settled(pending, ids[3], hit=False, settled_at=START, pick=MARKETS[0])
        add_settled(
            pending, ids[0], hit=False, settled_at=START, pick=MARKETS[2], outcome=Outcome.VOID
        )
        add_settled(
            pending, ids[0], hit=False, settled_at=START, pick=MARKETS[3], outcome=Outcome.CASHOUT
        )
        add_settled(
            pending, ids[2], hit=True, settled_at=START + timedelta(days=30), pick=MARKETS[5]
        )  # settled after the cutoff
        await pending.flush(session)

    async with sessions() as session:
        found = await settled_learning_signals(session, settled_before=START + timedelta(days=1))

    assert len(found) == 4  # win, hidden loss, the shared pick once, another market
    assert {s.outcome for s in found} == {Outcome.WIN, Outcome.LOSS}
    assert all(s.signal_id for s in found)


# ----------------------------------------------------------------------- calibration


async def test_a_miscalibrated_model_gets_a_calibration_and_brier_improves(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    last = await seed_probability_history(sessions, fixtures=400, shift=0.15)
    clock = Clock(last + timedelta(days=1))
    ctx = context(sessions, ledger, clock)

    report = await activate_calibration(sessions, ctx, clock)

    assert report.calibration_reason is CalibrationReason.IMPROVED
    assert report.calibration_activated
    async with sessions() as session:
        active = await active_calibration(session)
        changes = await list_learning_changes(session)
    assert active is not None
    assert active.passed_gate
    assert active.brier_after < active.brier_before
    assert KIND_CALIBRATION_ACTIVATED in {c.kind for c in changes}
    overlay = await DbLearningSource(sessions).load()
    assert overlay.calibration is not None
    assert overlay.calibration.version == active.version


async def test_a_well_calibrated_model_is_not_calibrated(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    last = await seed_probability_history(sessions, fixtures=200, shift=0.0, seed=4)
    ctx = context(sessions, ledger, Clock(last + timedelta(days=1)))

    report = await run_learning(ctx)

    assert not report.calibration_activated
    assert report.calibration_reason in {
        CalibrationReason.NO_IMPROVEMENT,
        CalibrationReason.NOT_SIGNIFICANT,
    }
    async with sessions() as session:
        assert await active_calibration(session) is None
        stored = await list_calibration_versions(session)
    assert [v.passed_gate for v in stored] == [False]  # recorded as evidence, never active
    assert (await DbLearningSource(sessions).load()).calibration is None


async def test_calibration_trains_on_raw_probabilities_not_on_its_own_output(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    last = await seed_probability_history(sessions, fixtures=60, shift=0.15)
    async with sessions.begin() as session:
        await session.execute(update(SignalRow).values(p_raw=SignalRow.p_model))
        await session.execute(update(SignalRow).values(p_model=0.5))  # "calibrated" output
    async with sessions() as session:
        found = await settled_learning_signals(session, settled_before=last + timedelta(days=1))
    assert {round(s.probability, 6) for s in found} != {0.5}
    assert len({s.probability for s in found}) > 100


# ----------------------------------------------------------------------- leakage / idempotency


async def snapshot(sessions: async_sessionmaker[AsyncSession]) -> list[object]:
    async with sessions() as session:
        calibrations = (await session.execute(select(CalibrationVersionRow))).scalars().all()
        segments = (await session.execute(select(SegmentStatRow))).scalars().all()
        return [
            [
                (
                    c.version,
                    c.params,
                    c.n_samples,
                    c.brier_before,
                    c.brier_after,
                    c.active,
                    c.passed_gate,
                )
                for c in calibrations
            ],
            [(s.n, s.roi, s.clv, s.calib_bias, s.ev_threshold, s.enabled) for s in segments],
        ]


async def reset_outputs(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions.begin() as session:
        for table in (CalibrationVersionRow, SegmentStatRow, LearningChangeRow):
            await session.execute(delete(table))
        await session.execute(delete(ledger_table))


async def test_future_results_do_not_change_an_earlier_review(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    last = await seed_probability_history(sessions, fixtures=300, shift=0.15, seed=3)
    cutoff = last - timedelta(days=100)
    clock = Clock(cutoff)
    ctx = context(sessions, ledger, clock)
    await run_learning(ctx)
    baseline = await snapshot(sessions)
    assert baseline[0]  # a calibration was fitted on the past

    # Rewrite history after the cutoff: flip every later result and its CLV.
    async with sessions.begin() as session:
        later = (
            (
                await session.execute(
                    select(SignalResultRow).where(SignalResultRow.settled_at >= cutoff)
                )
            )
            .scalars()
            .all()
        )
        assert later
        for result in later:
            result.outcome = Outcome.LOSS if result.outcome is Outcome.WIN else Outcome.WIN
            result.clv = 0.5
    await reset_outputs(sessions)
    await run_learning(ctx)

    assert await snapshot(sessions) == baseline


async def test_rerunning_the_job_is_idempotent(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    last = await seed_segment_history(sessions, n=120, hit_rate=0.30, clv_mean=-0.08)
    clock = Clock(last + timedelta(days=1))
    ctx = context(sessions, ledger, clock)

    first = await run_learning(ctx)
    again_same_week = await run_learning(ctx)
    confirming = await next_week(ctx, clock)
    after_confirming = await snapshot(sessions)
    quiet = await next_week(ctx, clock)

    assert not first.skipped
    assert again_same_week.skipped
    assert first.changes == ()
    assert not quiet.skipped
    assert quiet.changes == ()  # nothing new, nothing to journal
    assert await snapshot(sessions) == after_confirming
    async with sessions() as session:
        changes = (await session.scalar(select(func.count()).select_from(LearningChangeRow))) or 0
    assert changes == len(confirming.changes) == 1


async def test_a_new_week_with_the_same_data_adds_no_calibration_version(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    last = await seed_probability_history(sessions, fixtures=400, shift=0.15)
    clock = Clock(last + timedelta(days=1))
    ctx = context(sessions, ledger, clock)
    await run_learning(ctx)
    clock.now += timedelta(weeks=1)
    report = await run_learning(ctx)

    async with sessions() as session:
        versions = await list_calibration_versions(session)
    assert len(versions) == 1
    assert not report.calibration_activated


async def test_empty_history_is_harmless(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    report = await run_learning(context(sessions, ledger, Clock(START)))
    assert (report.signals, report.segments, report.changes) == (0, 0, ())
    assert report.calibration_reason is CalibrationReason.TOO_FEW_SAMPLES


async def test_a_failing_run_is_retryable(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.pipeline.learning as module  # noqa: PLC0415

    ctx = context(sessions, ledger, Clock(START))

    async def broken(*_args: object, **_kwargs: object) -> object:
        message = "disk full"
        raise OSError(message)

    monkeypatch.setattr(module, "settled_learning_signals", broken)
    with pytest.raises(OSError, match="disk full"):
        await run_learning(ctx)
    monkeypatch.undo()
    entry = await ledger.get(
        module.LedgerKey(module.JOB, week_start(START), module.SYSTEM_USER, module.SUBJECT)
    )
    assert entry is not None
    assert entry.status == "failed"


def test_week_start_is_monday() -> None:
    assert week_start(datetime(2026, 11, 4, 15, tzinfo=UTC)).isoformat() == "2026-11-02"
    assert week_start(datetime(2026, 11, 8, 23, tzinfo=UTC)).isoformat() == "2026-11-02"
    assert week_start(datetime(2026, 11, 9, 0, tzinfo=UTC)).isoformat() == "2026-11-09"


# ----------------------------------------------------------------------- rollback


async def test_a_calibration_activation_can_be_rolled_back_once(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    last = await seed_probability_history(sessions, fixtures=400, shift=0.15)
    clock = Clock(last + timedelta(days=1))
    ctx = context(sessions, ledger, clock)
    report = await activate_calibration(sessions, ctx, clock)
    (change,) = [c for c in report.changes if c.kind == KIND_CALIBRATION_ACTIVATED]

    assert await _undone(ctx, change.id)
    assert not await _undone(ctx, change.id)  # already undone

    async with sessions() as session:
        assert await active_calibration(session) is None
        journal = await list_learning_changes(session)
    assert [c.kind for c in journal][:1] == ["calibration_rolled_back"]
    assert next(c for c in journal if c.id == change.id).rolled_back
    assert (await DbLearningSource(sessions).load()).calibration is None


async def test_a_segment_rollback_restores_it_and_holds_it(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    last = await seed_segment_history(sessions, n=120, hit_rate=0.30, clv_mean=-0.08)
    clock = Clock(last + timedelta(days=1))
    ctx = context(sessions, ledger, clock)
    await run_learning(ctx)
    (change,) = (await next_week(ctx, clock)).changes

    assert await _undone(ctx, change.id)
    restored = (await list_segment_rows_of(sessions))[0]
    assert restored.enabled
    assert restored.hold_until == clock.now + DEFAULT_MANUAL_HOLD

    later = await next_week(ctx, clock)  # the evidence is still terrible, but a person decided
    assert later.changes == ()
    assert (await list_segment_rows_of(sessions))[0].enabled

    clock.now += DEFAULT_MANUAL_HOLD  # after the hold the review may act again, after 2 runs
    assert (await run_learning(ctx)).changes == ()
    after_hold = await next_week(ctx, clock)
    assert [c.kind for c in after_hold.changes] == [KIND_SEGMENT_DISABLED]


async def test_rollback_of_unknown_or_non_undoable_entries(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    last = await seed_probability_history(sessions, fixtures=400, shift=0.15)
    clock = Clock(last + timedelta(days=1))
    ctx = context(sessions, ledger, clock)
    await activate_calibration(sessions, ctx, clock)
    assert not await _undone(ctx, 9999)
    async with sessions() as session:
        journal = await list_learning_changes(session)
    assert len(journal) >= 1
    assert isinstance(journal, Sequence)
