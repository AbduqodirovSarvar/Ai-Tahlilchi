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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db._sql import table_of
from app.db.enums import Outcome, RecalcKind
from app.db.repo.records import RecalcTask, UserBetRecord
from app.db.schema import ScheduledRecalcRow, SignalResultRow, UserBetRow

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


async def complete_recalc(
    session: AsyncSession, task_id: int, *, now: datetime, claimed_at: datetime
) -> bool:
    """Mark a task done if the caller still owns it (``claimed_at`` is its claim time).

    ``False`` when the task is already done, does not exist, or its lease expired and
    another worker claimed it meanwhile: a slow first worker must not close that claim.
    """
    result = await session.execute(
        update(ScheduledRecalcRow)
        .where(
            ScheduledRecalcRow.id == task_id,
            ScheduledRecalcRow.done == false(),
            ScheduledRecalcRow.done_at == claimed_at,
        )
        .values(done=True, done_at=now)
    )
    return bool(getattr(result, "rowcount", 0))


async def release_recalc(session: AsyncSession, task_id: int, *, claimed_at: datetime) -> bool:
    """Give a task back (the job failed); it is claimable again at once.

    Only the current owner (matching ``claimed_at``) can release it.
    """
    result = await session.execute(
        update(ScheduledRecalcRow)
        .where(
            ScheduledRecalcRow.id == task_id,
            ScheduledRecalcRow.done == false(),
            ScheduledRecalcRow.done_at == claimed_at,
        )
        .values(done_at=None)
    )
    return bool(getattr(result, "rowcount", 0))


async def reschedule_recalc(
    session: AsyncSession, task_id: int, *, run_at: datetime, claimed_at: datetime
) -> bool:
    """Give a failed task back to run again at ``run_at`` (a backoff), not at once.

    Only the current owner (matching ``claimed_at``) can move it; ``False`` otherwise.
    """
    result = await session.execute(
        update(ScheduledRecalcRow)
        .where(
            ScheduledRecalcRow.id == task_id,
            ScheduledRecalcRow.done == false(),
            ScheduledRecalcRow.done_at == claimed_at,
        )
        .values(run_at=run_at, done_at=None)
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
    client_token: str | None = None,
) -> UserBetRecord:
    """Record a bet the user placed.

    With a ``client_token`` this is idempotent per ``(user_id, client_token)``: repeating it
    (a double tap, a redelivered update, a racing worker) returns the bet stored first and
    writes nothing, so one tap can never become two bets.
    """
    if client_token is not None:
        existing = await _bet_by_token(session, user_id, client_token)
        if existing is not None:
            return _bet(existing)
    row = UserBetRow(
        user_id=user_id,
        signal_id=signal_id,
        odds_taken=odds_taken,
        stake=stake,
        placed_at=placed_at,
        client_token=client_token,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        # A concurrent writer won the (user, token) race; anything else is a real error.
        existing = (
            None if client_token is None else await _bet_by_token(session, user_id, client_token)
        )
        if existing is None:
            raise
        return _bet(existing)
    return _bet(row)


async def get_bet_by_token(
    session: AsyncSession, user_id: int, client_token: str
) -> UserBetRecord | None:
    """The user's bet placed under ``client_token``, if any."""
    row = await _bet_by_token(session, user_id, client_token)
    return None if row is None else _bet(row)


async def _bet_by_token(
    session: AsyncSession, user_id: int, client_token: str
) -> UserBetRow | None:
    return await session.scalar(
        select(UserBetRow).where(
            UserBetRow.user_id == user_id, UserBetRow.client_token == client_token
        )
    )


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


async def bets_settled_between(
    session: AsyncSession, user_id: int, *, start: datetime, end: datetime
) -> list[UserBetRecord]:
    """A user's bets settled in ``[start, end)`` with a known profit, oldest first.

    The loss limits of the responsible-gaming rule read it: they are judged on the day a bet
    was *settled*, not placed. Money is summed in Python by the caller (ADR 0002).
    """
    rows = await session.scalars(
        select(UserBetRow)
        .where(
            UserBetRow.user_id == user_id,
            UserBetRow.settled_at.is_not(None),
            UserBetRow.profit.is_not(None),
            UserBetRow.settled_at >= start,
            UserBetRow.settled_at < end,
        )
        .order_by(UserBetRow.settled_at, UserBetRow.id)
    )
    return [_bet(row) for row in rows]


async def bets_for_signal(session: AsyncSession, signal_id: int) -> list[UserBetRecord]:
    """Every bet placed on a signal (settlement), oldest first."""
    rows = await session.scalars(
        select(UserBetRow).where(UserBetRow.signal_id == signal_id).order_by(UserBetRow.id)
    )
    return [_bet(row) for row in rows]


async def open_bets_of_settled_signals(session: AsyncSession) -> list[UserBetRecord]:
    """Unsettled bets whose signal already has a result (a bet placed after settlement).

    Settlement closes them with the stored result; normally there are none.
    """
    rows = await session.scalars(
        select(UserBetRow)
        .where(
            UserBetRow.outcome.is_(None),
            UserBetRow.signal_id.in_(select(SignalResultRow.signal_id)),
        )
        .order_by(UserBetRow.id)
    )
    return [_bet(row) for row in rows]


async def settle_bet(  # noqa: PLR0913 - one column per argument
    session: AsyncSession,
    bet_id: int,
    *,
    user_id: int,
    outcome: Outcome,
    profit: Decimal,
    settled_at: datetime,
) -> UserBetRecord | None:
    """Record the outcome and profit of the user's own bet (re-settling corrects it).

    ``None`` if the bet is unknown or belongs to another user.
    """
    row = await session.scalar(
        select(UserBetRow).where(UserBetRow.id == bet_id, UserBetRow.user_id == user_id)
    )
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
        client_token=row.client_token,
    )
