"""The weekly "what the model learned" message (F8.8): who gets it, once, and how it recovers."""

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.repo import add_learning_change, ensure_user, mark_change_rolled_back
from app.pipeline.context import PipelineContext
from app.pipeline.delivery import NullDelivery
from app.pipeline.learning import KIND_SEGMENT_DISABLED, LearningReport, week_start
from app.pipeline.learning_digest import (
    JOB,
    LearningDelivery,
    SummaryDelivery,
    announce_learning,
)
from app.pipeline.learning_settings import LearningMode, LearningSettings
from app.pipeline.ledger import Ledger, LedgerKey
from app.scheduler import JOB_LEARNING, PipelineJobs
from tests.pipeline.harness import Env
from tests.pipeline.test_learning import Clock

MONDAY_NINE = datetime(2026, 10, 19, 9, 5, tzinfo=UTC)  # a Monday, 14:05 in Tashkent
USERS = (1001, 1002, 1003)


@dataclass
class Recorder(NullDelivery):
    """Collects the summaries; ``failing`` users get ``None`` (not delivered) first."""

    sent: list[tuple[int, LearningDelivery, LedgerKey]] = field(default_factory=list)
    failing: set[int] = field(default_factory=set)

    async def learning_summary(
        self, user_id: int, message: LearningDelivery, key: LedgerKey
    ) -> str | None:
        if user_id in self.failing:
            return None
        self.sent.append((user_id, message, key))
        return "1:1"


def make_context(
    sessions: async_sessionmaker[AsyncSession],
    ledger: Ledger,
    clock: Clock,
    mode: LearningMode = LearningMode.SHADOW,
) -> PipelineContext:
    return PipelineContext(
        sessions=sessions,
        ledger=ledger,
        clock=clock,
        learning_settings=LearningSettings(mode=mode),
    )


async def seed_users(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions.begin() as session:
        for user_id in USERS:
            await ensure_user(session, user_id, timezone="Asia/Tashkent")


async def seed_change(
    sessions: async_sessionmaker[AsyncSession], now: datetime, *, applied: bool = True
) -> int:
    async with sessions.begin() as session:
        change = await add_learning_change(
            session,
            kind=KIND_SEGMENT_DISABLED,
            subject="39|totals|1.5-2.0",
            before={"enabled": True},
            after={"enabled": False, "ev_threshold": 0.0, "evidence": {"n": 80}},
            reason="roi_negative",
            now=now,
            applied=applied,
        )
    return change.id


def test_the_recorder_is_a_summary_delivery() -> None:
    assert isinstance(Recorder(), SummaryDelivery)
    assert not isinstance(NullDelivery(), SummaryDelivery)


async def test_every_user_is_told_once_with_the_weeks_journal(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    await seed_users(sessions)
    clock = Clock(MONDAY_NINE)
    change_id = await seed_change(sessions, MONDAY_NINE - timedelta(minutes=5))
    old = await seed_change(sessions, MONDAY_NINE - timedelta(days=9))
    recorder = Recorder()
    ctx = make_context(sessions, ledger, clock)

    told = await announce_learning(ctx, recorder)
    again = await announce_learning(ctx, recorder)  # a re-run of the job

    assert (told, again) == (3, 0)
    assert sorted(user for user, _, _ in recorder.sent) == list(USERS)
    for _, message, key in recorder.sent:
        assert [c.id for c in message.changes] == [change_id]  # last week's entry is not news
        assert old not in [c.id for c in message.changes]
        assert message.mode is LearningMode.SHADOW
        assert message.week == week_start(MONDAY_NINE)
        assert (key.job, key.subject) == (JOB, "weekly")


async def test_shadow_entries_are_passed_on_flagged_as_not_applied(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    await seed_users(sessions)
    await seed_change(sessions, MONDAY_NINE - timedelta(minutes=5), applied=False)
    recorder = Recorder()

    await announce_learning(make_context(sessions, ledger, Clock(MONDAY_NINE)), recorder)

    assert [c.applied for c in recorder.sent[0][1].changes] == [False]


async def test_a_quiet_week_still_sends_the_no_change_message(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    await seed_users(sessions)
    recorder = Recorder()

    await announce_learning(make_context(sessions, ledger, Clock(MONDAY_NINE)), recorder)

    assert len(recorder.sent) == len(USERS)
    assert all(message.changes == () for _, message, _ in recorder.sent)


async def test_off_sends_nothing(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    await seed_users(sessions)
    recorder = Recorder()

    told = await announce_learning(
        make_context(sessions, ledger, Clock(MONDAY_NINE), LearningMode.OFF), recorder
    )

    assert told == 0
    assert recorder.sent == []


async def test_a_delivery_without_the_summary_port_is_skipped_quietly(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    await seed_users(sessions)

    told = await announce_learning(
        make_context(sessions, ledger, Clock(MONDAY_NINE)), NullDelivery()
    )

    assert told == 0


async def test_quiet_hours_hold_the_message_back_for_that_user(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    await seed_users(sessions)
    three_am_tashkent = datetime(2026, 10, 19, 22, 0, tzinfo=UTC) - timedelta(days=1)
    # default quiet hours are 00:00-08:00 local; 22:00 UTC is 03:00 in Tashkent
    recorder = Recorder()
    ctx = make_context(sessions, ledger, Clock(three_am_tashkent))

    told = await announce_learning(ctx, recorder)

    assert told == 0
    assert recorder.sent == []
    # the key was not used up: the same call outside quiet hours delivers
    daytime = replace(ctx, clock=Clock(three_am_tashkent + timedelta(hours=6)))
    assert await announce_learning(daytime, recorder) == len(USERS)


async def test_one_unreachable_user_does_not_stop_the_others_and_is_retried(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    await seed_users(sessions)
    clock = Clock(MONDAY_NINE)
    recorder = Recorder(failing={1002})
    ctx = make_context(sessions, ledger, clock)

    first = await announce_learning(ctx, recorder)
    recorder.failing.clear()
    clock.now += timedelta(days=1)  # past any retry backoff
    second = await announce_learning(ctx, recorder)

    assert first == 2
    assert second == 1  # only the failed user, the others are done
    assert sorted(user for user, _, _ in recorder.sent) == list(USERS)


async def test_the_scheduler_job_records_the_run_and_announces(
    sessions: async_sessionmaker[AsyncSession],
    ledger: Ledger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_users(sessions)
    recorder = Recorder()
    ctx = make_context(sessions, ledger, Clock(MONDAY_NINE))

    async def fake_review(_: PipelineContext) -> LearningReport:
        return LearningReport(signals=12)

    monkeypatch.setattr("app.scheduler.run_learning", fake_review)

    await PipelineJobs(ctx, recorder).run(JOB_LEARNING)

    run = (await ledger.last_runs())[JOB_LEARNING]
    assert run.ok
    assert "signals=12" in (run.detail or "")
    assert len(recorder.sent) == len(USERS)


async def test_a_repeated_job_still_tells_users_who_were_missed_but_records_no_new_run(
    sessions: async_sessionmaker[AsyncSession],
    ledger: Ledger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The review finished, the process died before the messages: the next call finishes them."""
    await seed_users(sessions)
    recorder = Recorder()
    ctx = make_context(sessions, ledger, Clock(MONDAY_NINE))

    async def already_done(_: PipelineContext) -> LearningReport:
        return LearningReport(skipped=True)

    monkeypatch.setattr("app.scheduler.run_learning", already_done)

    await PipelineJobs(ctx, recorder).run(JOB_LEARNING)

    assert JOB_LEARNING not in await ledger.last_runs()
    assert len(recorder.sent) == len(USERS)


async def test_a_failing_review_sends_nothing(
    sessions: async_sessionmaker[AsyncSession],
    ledger: Ledger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_users(sessions)
    recorder = Recorder()
    ctx = make_context(sessions, ledger, Clock(MONDAY_NINE))

    async def broken(_: PipelineContext) -> LearningReport:
        raise RuntimeError("database is locked")

    monkeypatch.setattr("app.scheduler.run_learning", broken)

    await PipelineJobs(ctx, recorder).run(JOB_LEARNING)

    assert recorder.sent == []
    assert not (await ledger.last_runs())[JOB_LEARNING].ok


async def test_a_rolled_back_entry_is_reported_as_such(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    await seed_users(sessions)
    change_id = await seed_change(sessions, MONDAY_NINE - timedelta(minutes=5))
    async with sessions.begin() as session:
        await mark_change_rolled_back(session, change_id)
    recorder = Recorder()

    await announce_learning(make_context(sessions, ledger, Clock(MONDAY_NINE)), recorder)

    assert [c.rolled_back for c in recorder.sent[0][1].changes] == [True]


async def test_the_real_delivery_turns_the_journal_into_notifier_dtos(seeded_env: Env) -> None:
    """End to end over the production adapter: ``announce_learning`` -> notifier DTOs."""
    ctx = seeded_env.ctx
    now = seeded_env.clock()
    async with ctx.sessions.begin() as session:
        await ensure_user(session, 4242, timezone="Asia/Tashkent")
    await seed_change(ctx.sessions, now - timedelta(minutes=1), applied=False)
    wide_awake = replace(
        ctx,
        clock=Clock(now.replace(hour=12)),
        learning_settings=LearningSettings(mode=LearningMode.SHADOW),
    )

    told = await announce_learning(wide_awake, seeded_env.system.delivery)

    summaries = [s for s in seeded_env.notifier.sent if s.method == "learning_summary"]
    assert told == len(summaries) >= 1
    mine = next(s for s in summaries if s.user_id == 4242)
    summary = mine.payload
    assert summary.mode.value == "shadow"  # type: ignore[attr-defined]
    assert [c.applied for c in summary.changes] == [False]  # type: ignore[attr-defined]
    assert mine.key.job == JOB  # type: ignore[union-attr]
