"""T6 chaos: Telegram answers 429 / 403 / 400 / 5xx / network errors on every notifier path.

The real pipeline drives the real ``TelegramNotifier``; only the HTTP session misbehaves. Each
test checks the same three things: nothing raises out of a job, the pipeline's idempotency
state matches what was really delivered, and once Telegram recovers the user gets each message
exactly once (or, for a blocked user, nothing at all and no retry storm).
"""

import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import timedelta
from typing import Any, override

import pytest
from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import SendMessage, TelegramMethod

from app.scheduler import JOB_RECALC_TICK, JOB_REFRESH, JOB_SETTLE
from app.services.interfaces import (
    AdminAlert,
    BetDraft,
    DateSpec,
    DeliveryKey,
    LearningMode,
    LearningSummary,
)
from tests.bot.fakes import RecordingSession
from tests.e2e.t6_world import PERMISSIVE, SLEPT, USER, Week, at, jobs, open_week, tick

pytestmark = pytest.mark.slow

LONG_WAIT = 3600


def _forbidden(m: TelegramMethod[Any]) -> Exception:
    return TelegramForbiddenError(m, "Forbidden: bot was blocked by the user")


FAULTS: dict[str, Callable[[TelegramMethod[Any]], Exception]] = {
    "forbidden": _forbidden,
    "bad_request": lambda m: TelegramBadRequest(m, "Bad Request: can't parse entities"),
    "flood_short": lambda m: TelegramRetryAfter(m, "Too Many Requests", 1),
    "flood_long": lambda m: TelegramRetryAfter(m, "Too Many Requests", LONG_WAIT),
    "server": lambda m: TelegramServerError(m, "Internal Server Error"),
    "network": lambda m: TelegramNetworkError(m, "Request timeout error"),
}


class FaultSession(RecordingSession):
    """Fails ``sendMessage`` while ``kind`` is set (or on chosen request numbers)."""

    def __init__(self) -> None:
        super().__init__()
        self.kind: str | None = None
        self.fail_numbers: set[int] = set()
        self.calls = 0
        self.failures = 0

    @override
    async def make_request(
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        if isinstance(method, SendMessage):
            self.calls += 1
            if self.kind is not None or self.calls in self.fail_numbers:
                self.failures += 1
                raise FAULTS[self.kind or "bad_request"](method)
        return await super().make_request(bot, method, timeout)


@pytest.fixture
def fault_session() -> FaultSession:
    return FaultSession()


@pytest.fixture
def fault_bot(fault_session: FaultSession) -> Bot:
    return Bot(token="42:TEST", session=fault_session)


def digests(w: Week) -> list[SendMessage]:
    return [m for m in w.pushed(USER) if m.text.startswith("☀️")]


def cards(w: Week) -> list[SendMessage]:
    return [m for m in w.pushed(USER) if "🎯 Tanlov" in m.text]


@pytest.mark.parametrize("kind", sorted(FAULTS))
async def test_a_digest_during_a_telegram_outage_is_retried_and_arrives_once(
    history_template,
    tmp_path,
    fault_bot,
    fault_session,
    kind,  # type: ignore[no-untyped-def]
) -> None:
    async with open_week(history_template, tmp_path, fault_bot, fault_session) as w:
        await w.settings(USER, **PERMISSIVE)
        await jobs(w).run(JOB_REFRESH)
        fault_session.kind = kind
        await tick(w, 30)  # must not raise
        assert digests(w) == []
        assert fault_session.failures >= 1
        runs = await w.ctx.ledger.last_runs()
        assert all(run.ok for name, run in runs.items() if name != "daily")

        fault_session.kind = None  # Telegram is back
        await tick(w, 60 * 3)  # past the backoff of the failed attempt
        if kind == "forbidden":
            # the user blocked the bot: the key is closed, nobody retries, nothing is sent
            assert digests(w) == []
            assert fault_session.calls == fault_session.failures
        else:
            assert len(digests(w)) == 1
            assert len(cards(w)) == 10
        await tick(w, 60 * 6)
        assert len(digests(w)) == (0 if kind == "forbidden" else 1)  # never a second digest


async def test_one_flood_wait_is_slept_through_and_the_digest_still_arrives_once(
    history_template,
    tmp_path,
    fault_bot,
    fault_session,  # type: ignore[no-untyped-def]
) -> None:
    async with open_week(history_template, tmp_path, fault_bot, fault_session) as w:
        await w.settings(USER, **PERMISSIVE)
        await jobs(w).run(JOB_REFRESH)
        SLEPT.clear()
        # the very first sendMessage is flood-limited once; the retry goes through
        fault_session.fail_numbers = {1}
        fault_session.kind = None
        original = FAULTS["bad_request"]
        FAULTS["bad_request"] = FAULTS["flood_short"]
        try:
            await tick(w, 30)
        finally:
            FAULTS["bad_request"] = original
    assert SLEPT == [1.0]
    assert len(digests(w)) == 1
    assert len(cards(w)) == 10


async def test_the_pipeline_gives_up_after_five_failures_and_tells_the_admin_not_crashes(
    history_template,
    tmp_path,
    fault_bot,
    fault_session,  # type: ignore[no-untyped-def]
) -> None:
    async with open_week(history_template, tmp_path, fault_bot, fault_session) as w:
        await w.settings(USER, **PERMISSIVE)
        await jobs(w).run(JOB_REFRESH)
        fault_session.kind = "server"
        for _ in range(60):  # a long outage: ticks every 30 minutes for 30 hours
            await tick(w, 30)
        fault_session.kind = None
        await tick(w, 60 * 2)
        # five attempts spent, the key is exhausted: no digest today even after recovery ...
        assert digests(w) == []
        errors = await w.ctx.ledger.recent_errors()
        assert any("daily" in str(e) for e in errors)
        # ... but the signals were stored, so the user can still ask for them
        with closing(sqlite3.connect(tmp_path / "week.db")) as db:
            assert db.execute("SELECT count(*) FROM signals").fetchone()[0] == 10


@pytest.mark.xfail(
    strict=True,
    reason="T6 finding: TelegramNotifier._daily breaks out of the card loop at the first card "
    "Telegram rejects, so one bad card hides every later signal of the digest and the digest "
    "is still recorded as delivered (src/app/bot/notifier.py _daily: `break`)",
)
async def test_the_remaining_cards_are_still_sent_when_one_card_is_rejected(
    history_template,
    tmp_path,
    fault_bot,
    fault_session,  # type: ignore[no-untyped-def]
) -> None:
    async with open_week(history_template, tmp_path, fault_bot, fault_session) as w:
        await w.settings(USER, **PERMISSIVE)
        await jobs(w).run(JOB_REFRESH)
        # requests: 1 = digest header, 2 = day header, 3.. = cards; card #2 (request 4) is rejected
        fault_session.fail_numbers = {4}
        await tick(w, 30)
        assert len(cards(w)) == 9  # ten cards minus the rejected one


@pytest.mark.parametrize("kind", sorted(FAULTS))
async def test_a_settlement_notice_failure_never_loses_the_result(
    history_template,
    tmp_path,
    fault_bot,
    fault_session,
    kind,  # type: ignore[no-untyped-def]
) -> None:
    async with open_week(history_template, tmp_path, fault_bot, fault_session) as w:
        await w.settings(USER, **PERMISSIVE)
        await jobs(w).run(JOB_REFRESH)
        await tick(w, 30)
        signals = await w.services.signals.list_signals(USER, DateSpec.next_days(3))
        bet = await w.services.bets.place_bet(USER, BetDraft(signal_id=signals[0].signal_id))
        fault_session.kind = kind
        at(w, 22, 3, 0)
        await jobs(w).run(JOB_SETTLE)  # must not raise
        settled = await w.services.signals.get_signals(USER, [s.signal_id for s in signals])
        assert all(v.result is not None for v in settled)  # results are stored regardless
        assert (await w.services.bets.get_bet(USER, bet.bet_id)).profit is not None
        before = len([m for m in w.pushed(USER) if "💰 Natija:" in m.text])
        assert before == 0

        fault_session.kind = None
        for _ in range(8):  # later runs retry the unsent notices with a backoff
            w.clock.advance(timedelta(minutes=20))
            await jobs(w).run(JOB_SETTLE)
        notices = [m for m in w.pushed(USER) if "💰 Natija:" in m.text]
        if kind == "forbidden":
            assert notices == []
        else:
            assert len(notices) == len(signals)  # exactly once each


@pytest.mark.parametrize("kind", sorted(FAULTS))
async def test_a_scheduled_update_failure_is_retried_and_never_doubled(
    history_template,
    tmp_path,
    fault_bot,
    fault_session,
    kind,  # type: ignore[no-untyped-def]
) -> None:
    async with open_week(history_template, tmp_path, fault_bot, fault_session) as w:
        await w.settings(USER, **PERMISSIVE)
        await jobs(w).run(JOB_REFRESH)
        await tick(w, 30)
        fault_session.kind = kind
        for _ in range(6):
            await tick(w, 60)  # pre-match recalculations come due while Telegram misbehaves
        fault_session.kind = None
        for _ in range(12):
            await tick(w, 60)
        updates = [m for m in w.pushed(USER) if "Koeff.:" in m.text and "yangilandi" in m.text]
        per_signal: dict[str, int] = {}
        for m in updates:
            per_signal[m.text.splitlines()[0]] = per_signal.get(m.text.splitlines()[0], 0) + 1
        assert max(per_signal.values(), default=0) <= 2  # 24h and pre-match kinds, never doubled
        await jobs(w).run(JOB_RECALC_TICK)


@pytest.mark.parametrize("kind", sorted(FAULTS))
async def test_admin_alerts_and_the_weekly_summary_never_raise_whatever_telegram_says(
    history_template,
    tmp_path,
    fault_bot,
    fault_session,
    kind,  # type: ignore[no-untyped-def]
) -> None:
    async with open_week(history_template, tmp_path, fault_bot, fault_session) as w:
        fault_session.kind = kind
        key = DeliveryKey(job="x", day=w.clock().date(), user_id=USER, subject="t")
        await w.notifier.alert_admin(AdminAlert(component="job", message="boom <b>", at=w.clock()))
        summary = LearningSummary(week=w.clock().date(), mode=LearningMode.SHADOW, changes=())
        assert await w.notifier.send_learning_summary(USER, summary, key=key) is None
        fault_session.kind = None
        after = await w.notifier.send_learning_summary(USER, summary, key=key)
        # a blocked user stays skipped for 24 h (contract v1.6); anyone else is served again
        assert (after is None) == (kind == "forbidden")
