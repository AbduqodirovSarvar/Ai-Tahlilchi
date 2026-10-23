"""T6 chaos: kill the process at EVERY boundary of the daily digest, restart, count what arrived.

A boundary is one SQL statement, one commit, one Gemini request or one Telegram request of the
digest run. At boundary ``n`` the "process" dies (a ``BaseException`` that the pipeline's
``except Exception`` blocks cannot swallow, and which keeps firing at every later boundary so
no cleanup code gets to run, like a real ``kill -9``). The restart is a new engine, context,
notifier and dispatcher over the same files; the ticks then run until nothing changes.

Contract being verified (ADR 0008, "at least once, deduplicated by key"): the digest is never
lost, signals are never duplicated, and a repeated digest happens only when the death came
after something was already sent (documented at-least-once).
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, override

import pytest
from aiogram import Bot
from aiogram.methods import SendMessage, TelegramMethod
from sqlalchemy import event

from app.pipeline.daily import run_due_digests
from app.pipeline.ledger import LedgerKey
from app.scheduler import JOB_DAILY_TICK, JOB_REFRESH
from app.services.interfaces import DateSpec
from tests.bot.fakes import RecordingSession
from tests.e2e.t6_world import PERMISSIVE, USER, Week, emit, jobs, open_week

pytestmark = pytest.mark.slow

RECOVERY_TICKS = 6
RECOVERY_STEP = timedelta(minutes=20)  # longer than the 15-minute claim lease


class Crash(BaseException):
    """The process was killed here."""


@dataclass
class Killer:
    """Counts boundaries; kills at number ``at`` and at every boundary after it."""

    at: int | None = None
    count: int = 0
    dead: bool = False
    disabled: bool = False
    trail: list[str] = field(default_factory=list)

    def hit(self, what: str) -> None:
        if self.disabled:
            return
        if self.dead:
            raise Crash(what)
        self.count += 1
        self.trail.append(what)
        if self.at is not None and self.count == self.at:
            self.dead = True
            raise Crash(what)

    def attach(self, engine: Any) -> None:
        sync = engine.sync_engine

        @event.listens_for(sync, "before_cursor_execute")
        def _statement(conn, cursor, statement, *_rest):  # type: ignore[no-untyped-def]  # noqa: ARG001
            self.hit("sql:" + " ".join(statement.split())[:60])

        @event.listens_for(sync, "commit")
        def _commit(conn):  # type: ignore[no-untyped-def]  # noqa: ARG001
            self.hit("commit")


class KillableSession(RecordingSession):
    """A Telegram session that dies when the killer says so (before the request is recorded)."""

    def __init__(self, killer: Killer) -> None:
        super().__init__()
        self.killer = killer

    @override
    async def make_request(
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        self.killer.hit("telegram")
        return await super().make_request(bot, method, timeout)


@dataclass
class Outcome:
    n: int
    died_at: str
    digests: int
    cards: Counter[str]
    signals: frozenset[tuple[str, str, str]]
    versions: set[int]


def card_names(session: RecordingSession) -> Counter[str]:
    return Counter(
        m.text.splitlines()[0]
        for m in session.requests
        if isinstance(m, SendMessage) and m.chat_id == USER and "🎯 Tanlov" in m.text
    )


def digest_count(session: RecordingSession) -> int:
    return sum(
        isinstance(m, SendMessage) and m.chat_id == USER and m.text.startswith("☀️")
        for m in session.requests
    )


async def stored_signals(w: Week) -> tuple[frozenset[tuple[str, str, str]], set[int]]:
    views = await w.services.signals.list_signals(USER, DateSpec.next_days(3))
    return (
        frozenset(
            (f"{v.home_team}-{v.away_team}", v.market.type.value, v.selection.value) for v in views
        ),
        {v.version for v in views},
    )


async def one_run(history, tmp: Path, at: int | None) -> tuple[Outcome, list[str]]:  # type: ignore[no-untyped-def]
    """Run the digest with a kill at boundary ``at`` (None: no kill), restart, recover."""
    killer = Killer(at=at)
    session = KillableSession(killer)
    bot = Bot(token="42:TEST", session=session)
    async with open_week(history, tmp, bot, session) as w:
        await w.settings(USER, **PERMISSIVE)
        await jobs(w).run(JOB_REFRESH)
        w.gemini.hook = killer.hit
        killer.attach(w.env.engine)
        killer.disabled = False
        died = ""
        try:
            await run_due_digests(w.ctx, w.system.delivery)
        except Crash as crash:
            died = str(crash)
        trail = list(killer.trail)
        # the process is gone: its connections close, its uncommitted work vanishes
        killer.disabled = True
        await w.env.engine.dispose()
        reborn = await w.restart()
        reborn.gemini.hook = None
        for _ in range(RECOVERY_TICKS):
            reborn.clock.advance(RECOVERY_STEP)
            await jobs(reborn).run(JOB_DAILY_TICK)
        signals, versions = await stored_signals(reborn)
        outcome = Outcome(
            n=at or 0,
            died_at=died,
            digests=digest_count(session),
            cards=card_names(session),
            signals=signals,
            versions=versions,
        )
        done = await reborn.ctx.ledger.is_done(_digest_key())
        assert done, f"the digest key must end up done (kill at {at}: {died})"
    return outcome, trail


def _digest_key() -> LedgerKey:
    """The idempotency key of USER's digest of the seeded day (local 21.10.2026)."""
    return LedgerKey("daily", date(2026, 10, 21), USER, "digest")


async def test_killing_the_process_at_every_boundary_never_loses_or_duplicates_signals(
    history_template,
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    (tmp_path / "base").mkdir()
    baseline, trail = await one_run(history_template, tmp_path / "base", None)
    total = len(trail)
    assert baseline.digests == 1
    assert sum(baseline.cards.values()) == 10
    assert total > 20, "the run must cross many boundaries"

    rows = []
    for n in range(1, total + 1):
        folder = tmp_path / f"kill{n}"
        folder.mkdir()
        outcome, _ = await one_run(history_template, folder, n)
        rows.append(outcome)
        # never lost
        assert outcome.digests >= 1, (n, outcome.died_at)
        assert set(outcome.cards) >= set(baseline.cards), (n, outcome.died_at)
        # never a second copy of a signal, never a changed one
        assert outcome.signals == baseline.signals, (n, outcome.died_at)
        assert outcome.versions == {1}, (n, outcome.died_at)
        # at most one repeat of the digest (documented at-least-once)
        assert outcome.digests <= 2, (n, outcome.died_at)
        assert max(outcome.cards.values()) <= 2, (n, outcome.died_at)

    repeated = [r for r in rows if r.digests > 1 or max(r.cards.values()) > 1]
    emit(
        f"T6-CHAOS kill-drill: boundaries={total} runs={len(rows)} "
        f"exactly-once={len(rows) - len(repeated)} at-least-once-repeats={len(repeated)}"
    )
    for r in repeated:
        emit(f"T6-CHAOS   repeat after kill at #{r.n} ({r.died_at}): digests={r.digests}")
    # a repeat is only acceptable when the death came after the first message went out
    first_send = trail.index("telegram") + 1
    assert all(r.n > first_send for r in repeated), [r.n for r in repeated]
