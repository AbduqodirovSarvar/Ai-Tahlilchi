"""Fix round: pre-match and result messages that fail, are unconfirmed or give up."""

from datetime import timedelta
from pathlib import Path

from app.db.repo import signal_versions
from app.pipeline.config import PipelineConfig
from app.pipeline.ledger import MAX_ATTEMPTS, backoff
from app.pipeline.prematch import run_due_recalcs
from app.pipeline.settle import run_settlement
from tests.integration.t4_support import USER, FlakyNotifier, open_bare, permissive
from tests.integration.test_t4_hand_league import KICKOFF, MODEL_WEIGHT, WORLD_A, batch
from tests.integration.test_t4_resilience import (
    EARLIER,
    YESTERDAY_NOON,
    fresh_prices,
    schedule_tomorrow,
    two_finished_signals,
)

CONFIG = PipelineConfig(model_weight=MODEL_WEIGHT)
FOREVER = 100


async def test_a_failed_prematch_task_backs_off_and_adds_no_identical_versions(
    tmp_path: Path,
) -> None:
    notifier = FlakyNotifier()
    notifier.fail["signal_update"] = 2
    async with open_bare(
        tmp_path / "pre.db",
        batch(WORLD_A, EARLIER),
        config=CONFIG,
        notifier=notifier,
        now=YESTERDAY_NOON,
    ) as bare:
        signal_id = await schedule_tomorrow(bare, odds_move=False)
        bare.clock.now = KICKOFF - timedelta(minutes=55)
        await fresh_prices(bare, bare.clock() - timedelta(minutes=5))

        failures = [(await run_due_recalcs(bare.ctx, bare.system.delivery)).failed]
        for _ in range(5):  # every minute inside the backoff: nothing is attempted
            bare.clock.advance(timedelta(seconds=5))
            failures.append((await run_due_recalcs(bare.ctx, bare.system.delivery)).failed)
        bare.clock.advance(backoff(1))
        failures.append((await run_due_recalcs(bare.ctx, bare.system.delivery)).failed)
        bare.clock.advance(backoff(2))
        delivered = await run_due_recalcs(bare.ctx, bare.system.delivery)
        async with bare.sessions() as session:
            versions = await signal_versions(session, signal_id)

    assert failures == [1, 0, 0, 0, 0, 0, 1]  # two real failures, none in between
    assert delivered.notified == 1
    assert notifier.count("signal_update") == 1
    assert len(versions) == 1  # same numbers every time: not a single identical version added


async def test_an_unconfirmed_prematch_message_is_retried(tmp_path: Path) -> None:
    """A notifier that answers ``None`` (bot not attached) has not delivered anything."""
    notifier = FlakyNotifier()
    async with open_bare(
        tmp_path / "none.db",
        batch(WORLD_A, EARLIER),
        config=CONFIG,
        notifier=notifier,
        now=YESTERDAY_NOON,
    ) as bare:
        await schedule_tomorrow(bare, odds_move=False)
        bare.clock.now = KICKOFF - timedelta(minutes=55)
        await fresh_prices(bare, bare.clock() - timedelta(minutes=5))
        original = notifier.send_signal_update

        async def unconfirmed(*args, **kwargs):  # type: ignore[no-untyped-def]
            await original(*args, **kwargs)

        notifier.send_signal_update = unconfirmed  # type: ignore[method-assign]
        first = await run_due_recalcs(bare.ctx, bare.system.delivery)
        notifier.send_signal_update = original  # type: ignore[method-assign]
        bare.clock.advance(backoff(1))
        second = await run_due_recalcs(bare.ctx, bare.system.delivery)

    assert first.failed == 1
    assert second.notified == 1


async def test_a_prematch_task_gives_up_after_the_attempt_limit_and_alerts(
    tmp_path: Path,
) -> None:
    notifier = FlakyNotifier()
    notifier.fail["signal_update"] = FOREVER
    async with open_bare(
        tmp_path / "giveup.db",
        batch(WORLD_A, EARLIER),
        config=CONFIG,
        notifier=notifier,
        now=YESTERDAY_NOON,
    ) as bare:
        await schedule_tomorrow(bare, odds_move=False)
        bare.clock.now = KICKOFF - timedelta(minutes=55)
        await fresh_prices(bare, bare.clock() - timedelta(minutes=5))
        reports = []
        for attempt in range(1, MAX_ATTEMPTS + 3):
            reports.append(await run_due_recalcs(bare.ctx, bare.system.delivery))
            bare.clock.advance(backoff(attempt))

    assert sum(r.failed for r in reports) == MAX_ATTEMPTS * 1  # only the pre-match task is live
    alerts = [n for n in notifier.sent if n.method == "alert_admin"]
    assert len(alerts) == 1
    assert "gave up" in alerts[0].payload.message  # type: ignore[attr-defined]


async def test_a_result_message_gives_up_after_the_attempt_limit_and_alerts(
    tmp_path: Path,
) -> None:
    notifier = FlakyNotifier()
    notifier.fail["result_settled"] = FOREVER
    async with open_bare(
        tmp_path / "result.db", None, config=CONFIG, notifier=notifier, now=KICKOFF.replace(hour=8)
    ) as bare:
        await permissive(bare.system.services, USER)
        await two_finished_signals(bare)
        reports = []
        for attempt in range(1, MAX_ATTEMPTS + 3):
            reports.append(await run_settlement(bare.ctx, bare.system.delivery))
            bare.clock.advance(backoff(attempt))

    assert reports[0].settled == 2  # both results are stored whatever happens to the messages
    assert sum(r.settled for r in reports) == 2
    attempts = notifier.fail["result_settled"]
    assert FOREVER - attempts == 2 * MAX_ATTEMPTS  # each message was tried the limit, then dropped
    alerts = [n for n in notifier.sent if n.method == "alert_admin"]
    assert len(alerts) == 1


async def test_an_unconfirmed_result_message_is_retried(tmp_path: Path) -> None:
    notifier = FlakyNotifier()
    async with open_bare(
        tmp_path / "result-none.db",
        None,
        config=CONFIG,
        notifier=notifier,
        now=KICKOFF.replace(hour=8),
    ) as bare:
        await permissive(bare.system.services, USER)
        await two_finished_signals(bare)
        original = notifier.send_result_settled
        calls = []

        async def unconfirmed(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            calls.append(1)

        notifier.send_result_settled = unconfirmed  # type: ignore[method-assign]
        first = await run_settlement(bare.ctx, bare.system.delivery)
        notifier.send_result_settled = original  # type: ignore[method-assign]
        bare.clock.advance(backoff(1))
        second = await run_settlement(bare.ctx, bare.system.delivery)

    assert first.announce_failed == 2
    assert len(calls) == 2
    assert second.notified == 2
    assert notifier.count("result_settled") == 2
