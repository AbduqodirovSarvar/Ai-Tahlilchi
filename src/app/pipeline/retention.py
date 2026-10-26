"""Retention of the price history (``odds_snapshots``), run once a week.

Every refresh, recalculation and pre-match check appends a full set of quotes, so the table
grows by far the most of all. What the system keeps for good:

* ``opening`` and ``closing`` snapshots (line movement, CLV, the backtest),
* every snapshot of a fixture that a signal exists for (the card's price history, post-mortems).

What is dropped is the intermediate ``morning``/``recalc``/``prematch`` captures of finished
matches that nobody decided on, once the match is older than :data:`RETENTION_DAYS`.

The same weekly run also prunes the provider call log (``provider_calls``: prompts and bodies,
the second heavy table) older than ``PipelineConfig.call_log_retention``.

Failure modes. The job claims a weekly ledger key, so a second call in the same week does
nothing and a crash lets the lease expire for a retry. Deletes run in batches of
:data:`DELETE_BATCH`, each in its own short transaction (SQLite allows one writer, and a long
delete would stall the bot); a crash between batches leaves a valid, merely not yet shrunk
table, and the next run continues. No ``VACUUM``: freed pages are reused by later inserts, and a
rebuild of the file is an operator action (``sqlite_tool``), not a weekly job.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repo import purge_calls_older_than
from app.db.schema import FixtureRow, OddsSnapshotRow, SignalRow
from app.domain import FixtureStatus, OddsKind
from app.logging import get_logger
from app.pipeline.context import PipelineContext
from app.pipeline.learning import week_start
from app.pipeline.ledger import SYSTEM_USER, LedgerKey

logger = get_logger(__name__)

JOB: Final = "retention"
SUBJECT: Final = "odds_snapshots"
RETENTION_DAYS: Final = 90
DELETE_BATCH: Final = 5_000
MAX_BATCHES: Final = 200
"""A run deletes at most this many batches (1 000 000 rows); the next week continues."""
_DISPOSABLE_KINDS: Final = (OddsKind.MORNING, OddsKind.RECALC, OddsKind.PREMATCH)
_FINISHED: Final = (FixtureStatus.FINISHED, FixtureStatus.CANCELLED, FixtureStatus.ABANDONED)


@dataclass(frozen=True, slots=True)
class RetentionReport:
    """What one retention run did."""

    skipped: bool = False
    deleted: int = 0
    calls_deleted: int = 0


async def run_retention(ctx: PipelineContext) -> RetentionReport:
    """Prune old intermediate price snapshots (see the module docstring); once per week."""
    now = ctx.clock()
    key = LedgerKey(JOB, week_start(now), SYSTEM_USER, SUBJECT)
    if not await ctx.ledger.claim(key, now=now):
        logger.info("retention_skipped", reason="already_done_this_week")
        return RetentionReport(skipped=True)
    cutoff = now - timedelta(days=RETENTION_DAYS)
    calls_cutoff = now - ctx.config.call_log_retention
    try:
        deleted = await _in_batches(lambda session: _delete_batch(session, cutoff), ctx)
        calls_deleted = await _in_batches(
            lambda session: purge_calls_older_than(session, calls_cutoff, limit=DELETE_BATCH), ctx
        )
    except Exception as error:
        await ctx.ledger.fail(key, now=ctx.clock(), detail=repr(error)[:200])
        raise
    detail = f"deleted={deleted} calls_deleted={calls_deleted}"
    await ctx.ledger.complete(key, now=ctx.clock(), detail=detail)
    logger.info(
        "retention_done",
        job=JOB,
        deleted=deleted,
        calls_deleted=calls_deleted,
        cutoff=cutoff.isoformat(),
        calls_cutoff=calls_cutoff.isoformat(),
    )
    return RetentionReport(deleted=deleted, calls_deleted=calls_deleted)


async def _in_batches(
    delete_batch: Callable[[AsyncSession], Awaitable[int]], ctx: PipelineContext
) -> int:
    """Run ``delete_batch`` in short transactions until a batch comes back short; the total."""
    total = 0
    for _ in range(MAX_BATCHES):
        async with ctx.sessions.begin() as session:
            batch = await delete_batch(session)
        total += batch
        if batch < DELETE_BATCH:
            break
    return total


async def _delete_batch(session: AsyncSession, cutoff: datetime) -> int:
    """Delete up to :data:`DELETE_BATCH` disposable snapshots; the number deleted."""
    with_signals = select(SignalRow.fixture_id)
    old_finished = select(FixtureRow.id).where(
        FixtureRow.kickoff_at < cutoff,
        FixtureRow.status.in_(_FINISHED),
        FixtureRow.id.not_in(with_signals),
    )
    ids = (
        select(OddsSnapshotRow.id)
        .where(
            OddsSnapshotRow.kind.in_(_DISPOSABLE_KINDS),
            OddsSnapshotRow.fixture_id.in_(old_finished),
        )
        .limit(DELETE_BATCH)
    )
    result = await session.execute(delete(OddsSnapshotRow).where(OddsSnapshotRow.id.in_(ids)))
    return int(getattr(result, "rowcount", 0))
