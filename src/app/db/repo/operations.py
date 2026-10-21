"""Scheduled recalculations (a small persistent job queue) and the user's bets.

Recalc claiming is lease-based so a crash never loses a job and two workers never run the
same one: :func:`claim_due_recalcs` stamps ``done_at`` as the *claim time* of a task that
is not ``done`` (an expired claim, older than ``lease``, may be claimed again);
:func:`complete_recalc` sets ``done``; :func:`release_recalc` gives a task back. The claim
is one conditional ``UPDATE ... RETURNING`` in the caller's transaction (SQLite ≥ 3.35 and
PostgreSQL), so racing claims cannot both win.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

from sqlalchemy import ColumnElement, and_, false, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db._sql import table_of
from app.db.enums import Outcome, RecalcKind
from app.db.repo.records import RecalcTask, UserBetRecord
from app.db.schema import ScheduledRecalcRow, UserBetRow

DEFAULT_CLAIM_LEASE: Final = timedelta(minutes=15)
DEFAULT_CLAIM_LIMIT: Final = 50
DEFAULT_BET_LIMIT: Final = 20

# --------------------------------------------------------------------------- recalcs


async def enqueue_recalc(
    session: AsyncSession, signal_id: int, *, kind: RecalcKind, run_at: datetime
) -> RecalcTask:
    """Schedule (or move) the ``kind`` recalculation of a signal.

    Idempotent: enqueuing the same signal, kind and time again changes nothing (even if
    it already ran). A different time (kick-off moved) re-opens the task at the new time.
    """
    row = await session.scalar(
        select(ScheduledRecalcRow).where(
            ScheduledRecalcRow.signal_id == signal_id, ScheduledRecalcRow.kind == kind
        )
    )
    if row is None:
        row = ScheduledRecalcRow(signal_id=signal_id, kind=kind, run_at=run_at, done=False)
        session.add(row)
    elif row.run_at != run_at:
        row.run_at, row.done, row.done_at = run_at, False, None
    await session.flush()
    return _task(row)


def _claimable(now: datetime, lease: timedelta) -> ColumnElement[bool]:
    return and_(
        ScheduledRecalcRow.done == false(),
        ScheduledRecalcRow.run_at <= now,
        or_(ScheduledRecalcRow.done_at.is_(None), ScheduledRecalcRow.done_at < now - lease),
    )


async def claim_due_recalcs(
    session: AsyncSession,
    *,
    now: datetime,
    limit: int = DEFAULT_CLAIM_LIMIT,
    lease: timedelta = DEFAULT_CLAIM_LEASE,
) -> list[RecalcTask]:
    """Claim up to ``limit`` due tasks (earliest first); each task is returned to one caller."""
    candidates = list(
        await session.scalars(
            select(ScheduledRecalcRow.id)
            .where(_claimable(now, lease))
            .order_by(ScheduledRecalcRow.run_at, ScheduledRecalcRow.id)
            .limit(limit)
        )
    )
    if not candidates:
        return []
    table = table_of(ScheduledRecalcRow)
    claimed = await session.scalars(
        update(table)
        .where(table.c.id.in_(candidates), _claimable(now, lease))
        .values(done_at=now)
        .returning(table.c.id)
    )
    ids = list(claimed)
    rows = await session.scalars(
        select(ScheduledRecalcRow)
        .where(ScheduledRecalcRow.id.in_(ids))
        .order_by(ScheduledRecalcRow.run_at, ScheduledRecalcRow.id)
        .execution_options(populate_existing=True)
    )
    return [_task(row) for row in rows]


async def complete_recalc(session: AsyncSession, task_id: int, *, now: datetime) -> bool:
    """Mark a claimed task done; ``False`` if it was already done or does not exist."""
    result = await session.execute(
        update(ScheduledRecalcRow)
        .where(ScheduledRecalcRow.id == task_id, ScheduledRecalcRow.done == false())
        .values(done=True, done_at=now)
    )
    return bool(getattr(result, "rowcount", 0))


async def release_recalc(session: AsyncSession, task_id: int) -> bool:
    """Give a claimed task back (the job failed); it is claimable again at once."""
    result = await session.execute(
        update(ScheduledRecalcRow)
        .where(ScheduledRecalcRow.id == task_id, ScheduledRecalcRow.done == false())
        .values(done_at=None)
    )
    return bool(getattr(result, "rowcount", 0))


async def cancel_recalcs(session: AsyncSession, signal_id: int, *, now: datetime) -> int:
    """Mark every pending task of a signal done (signal cancelled/hidden); returns how many."""
    result = await session.execute(
        update(ScheduledRecalcRow)
        .where(ScheduledRecalcRow.signal_id == signal_id, ScheduledRecalcRow.done == false())
        .values(done=True, done_at=now)
    )
    return int(getattr(result, "rowcount", 0))


def _task(row: ScheduledRecalcRow) -> RecalcTask:
    return RecalcTask(
        id=row.id,
        signal_id=row.signal_id,
        kind=row.kind,
        run_at=row.run_at,
        done=row.done,
        claimed_at=None if row.done else row.done_at,
    )


# --------------------------------------------------------------------------- bets


async def create_bet(  # noqa: PLR0913 - one column per argument
    session: AsyncSession,
    *,
    user_id: int,
    signal_id: int | None,
    odds_taken: float,
    stake: Decimal,
    placed_at: datetime,
) -> UserBetRecord:
    """Record a bet the user placed."""
    row = UserBetRow(
        user_id=user_id,
        signal_id=signal_id,
        odds_taken=odds_taken,
        stake=stake,
        placed_at=placed_at,
    )
    session.add(row)
    await session.flush()
    return _bet(row)


async def list_bets(
    session: AsyncSession, user_id: int, *, open_only: bool = False, limit: int = DEFAULT_BET_LIMIT
) -> list[UserBetRecord]:
    """A user's bets, newest first; ``open_only`` = not settled yet."""
    query = select(UserBetRow).where(UserBetRow.user_id == user_id)
    if open_only:
        query = query.where(UserBetRow.outcome.is_(None))
    rows = await session.scalars(
        query.order_by(UserBetRow.placed_at.desc(), UserBetRow.id.desc()).limit(limit)
    )
    return [_bet(row) for row in rows]


async def bets_placed_between(
    session: AsyncSession, user_id: int, *, start: datetime, end: datetime
) -> list[UserBetRecord]:
    """A user's bets placed in ``[start, end)`` (loss limits, bank history), oldest first.

    Money is summed in Python by the caller (SQLite stores money as text; ADR 0002).
    """
    rows = await session.scalars(
        select(UserBetRow)
        .where(
            UserBetRow.user_id == user_id,
            UserBetRow.placed_at >= start,
            UserBetRow.placed_at < end,
        )
        .order_by(UserBetRow.placed_at, UserBetRow.id)
    )
    return [_bet(row) for row in rows]


async def bets_for_signal(session: AsyncSession, signal_id: int) -> list[UserBetRecord]:
    """Every bet placed on a signal (settlement), oldest first."""
    rows = await session.scalars(
        select(UserBetRow).where(UserBetRow.signal_id == signal_id).order_by(UserBetRow.id)
    )
    return [_bet(row) for row in rows]


async def settle_bet(
    session: AsyncSession,
    bet_id: int,
    *,
    outcome: Outcome,
    profit: Decimal,
    settled_at: datetime,
) -> UserBetRecord | None:
    """Record a bet's outcome and profit (re-settling corrects it); ``None`` if unknown."""
    row = await session.get(UserBetRow, bet_id)
    if row is None:
        return None
    row.outcome, row.profit, row.settled_at = outcome, profit, settled_at
    await session.flush()
    return _bet(row)


def _bet(row: UserBetRow) -> UserBetRecord:
    return UserBetRecord(
        id=row.id,
        user_id=row.user_id,
        signal_id=row.signal_id,
        odds_taken=row.odds_taken,
        stake=row.stake,
        outcome=row.outcome,
        profit=row.profit,
        placed_at=row.placed_at,
        settled_at=row.settled_at,
    )
