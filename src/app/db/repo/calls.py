"""The provider call log (``provider_calls``): write, page through, read one, prune, summarise.

Lists never load the (up to 20 000 characters) response: the heavy columns are read only by
:func:`get_call`. Functions run in the caller's transaction (they flush, never commit); the
recorder in :mod:`app.db.call_log` owns its own short transaction.
"""

from datetime import datetime
from typing import Final

from sqlalchemy import ColumnElement, Integer, case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repo.records import CallDetail, CallStat, CallSummary
from app.db.schema import ProviderCallRow
from app.domain import CallOutcome, CallRecord, Provider

REQUEST_PREVIEW_CHARS: Final = 200
_REACHED_PROVIDER: Final = (CallOutcome.OK, CallOutcome.ERROR)


async def record_call(session: AsyncSession, call: CallRecord) -> int:
    """Store ``call``; its new id. The text is stored as given (the recorder scrubs it)."""
    row = ProviderCallRow(
        at=call.at,
        provider=call.provider,
        operation=call.operation,
        outcome=call.outcome,
        duration_ms=call.duration_ms,
        request=call.request,
        response=call.response,
        http_status=call.http_status,
        quota_cost=call.quota_cost,
        size_bytes=call.size_bytes,
        error=call.error,
    )
    session.add(row)
    await session.flush()
    return row.id


def _conditions(
    provider: Provider | None, outcome: CallOutcome | None
) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    if provider is not None:
        conditions.append(ProviderCallRow.provider == provider)
    if outcome is not None:
        conditions.append(ProviderCallRow.outcome == outcome)
    return conditions


async def list_calls(
    session: AsyncSession,
    *,
    provider: Provider | None = None,
    outcome: CallOutcome | None = None,
    limit: int,
    offset: int,
) -> list[CallSummary]:
    """A page of calls, newest first, without response text (see :class:`CallSummary`)."""
    query = (
        select(
            ProviderCallRow.id,
            ProviderCallRow.at,
            ProviderCallRow.provider,
            ProviderCallRow.operation,
            ProviderCallRow.outcome,
            ProviderCallRow.duration_ms,
            ProviderCallRow.http_status,
            ProviderCallRow.quota_cost,
            ProviderCallRow.size_bytes,
            ProviderCallRow.error,
            func.substr(ProviderCallRow.request, 1, REQUEST_PREVIEW_CHARS),
        )
        .where(*_conditions(provider, outcome))
        .order_by(ProviderCallRow.at.desc(), ProviderCallRow.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = await session.execute(query)
    return [
        CallSummary(
            id=row[0],
            at=row[1],
            provider=row[2],
            operation=row[3],
            outcome=row[4],
            duration_ms=row[5],
            http_status=row[6],
            quota_cost=row[7],
            size_bytes=row[8],
            error=row[9],
            request_preview=row[10],
        )
        for row in rows
    ]


async def count_calls(
    session: AsyncSession,
    *,
    provider: Provider | None = None,
    outcome: CallOutcome | None = None,
) -> int:
    """How many calls match the filters (for the pager)."""
    query = select(func.count(ProviderCallRow.id)).where(*_conditions(provider, outcome))
    return int(await session.scalar(query) or 0)


async def get_call(session: AsyncSession, call_id: int) -> CallDetail | None:
    """One call with its full request and response; ``None`` when it is unknown or pruned."""
    row = await session.get(ProviderCallRow, call_id)
    if row is None:
        return None
    return CallDetail(
        id=row.id,
        at=row.at,
        provider=row.provider,
        operation=row.operation,
        outcome=row.outcome,
        duration_ms=row.duration_ms,
        http_status=row.http_status,
        quota_cost=row.quota_cost,
        size_bytes=row.size_bytes,
        error=row.error,
        request=row.request,
        response=row.response,
    )


async def purge_calls_older_than(
    session: AsyncSession, cutoff: datetime, *, limit: int | None = None
) -> int:
    """Delete calls made before ``cutoff``; how many went.

    ``limit`` bounds one statement (SQLite has one writer, so a huge delete would stall it);
    callers loop until fewer than ``limit`` rows are deleted.
    """
    condition: ColumnElement[bool] = ProviderCallRow.at < cutoff
    if limit is not None:
        oldest = select(ProviderCallRow.id).where(condition).limit(limit)
        condition = ProviderCallRow.id.in_(oldest)
    result = await session.execute(delete(ProviderCallRow).where(condition))
    return int(getattr(result, "rowcount", 0))


def _count_of(kind: CallOutcome) -> ColumnElement[int]:
    """SQL ``SUM`` that counts the rows of one outcome (0, not NULL, for an empty group)."""
    counted = func.sum(case((ProviderCallRow.outcome == kind, 1), else_=0))
    return func.coalesce(counted, 0).cast(Integer)


async def call_stats(session: AsyncSession, since: datetime) -> list[CallStat]:
    """Per provider and operation, the calls made at or after ``since`` (one grouped query)."""
    rows = await session.execute(
        select(
            ProviderCallRow.provider,
            ProviderCallRow.operation,
            func.count(ProviderCallRow.id),
            _count_of(CallOutcome.ERROR),
            _count_of(CallOutcome.CACHED),
            _count_of(CallOutcome.QUOTA_BLOCKED),
            func.avg(
                case((ProviderCallRow.outcome.in_(_REACHED_PROVIDER), ProviderCallRow.duration_ms))
            ),
            func.coalesce(func.sum(ProviderCallRow.quota_cost), 0).cast(Integer),
        )
        .where(ProviderCallRow.at >= since)
        .group_by(ProviderCallRow.provider, ProviderCallRow.operation)
        .order_by(ProviderCallRow.provider, ProviderCallRow.operation)
    )
    return [
        CallStat(
            provider=row[0],
            operation=row[1],
            calls=row[2],
            errors=row[3],
            cached=row[4],
            blocked=row[5],
            avg_duration_ms=None if row[6] is None else float(row[6]),
            quota_cost=row[7],
        )
        for row in rows
    ]
