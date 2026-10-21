"""Daily API usage counters in the ``api_usage`` table.

:class:`SqlUsageStore` implements :class:`app.clients.quota.UsageStore`. Column meaning
(ADR 0007): ``requests_used`` = every request charged that UTC day; ``reserved_manual`` =
how many of them were manual (charged to the manual reserve). The reserve *size* is not
stored: it is policy (limit x share) and may change without rewriting history.

Each operation commits its own short transaction: a request that was sent must stay
counted even if the caller's unit of work later rolls back.
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.quota import UsageCounts, UsageKey
from app.db._sql import dialect_insert, table_of
from app.db.schema import ApiUsageRow, utcnow


@dataclass(frozen=True, slots=True)
class ApiUsageRecord:
    """One stored counter row."""

    key: UsageKey
    counts: UsageCounts


class SqlUsageStore:
    """:class:`~app.clients.quota.UsageStore` persisted in ``api_usage``."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        """Use ``sessions`` to open one short transaction per operation."""
        self._sessions = sessions

    async def get(self, key: UsageKey) -> UsageCounts:
        """See :meth:`app.clients.quota.UsageStore.get`."""
        async with self._sessions() as session:
            return await _counts(session, key)

    async def add(self, key: UsageKey, *, requests: int, manual: int) -> UsageCounts:
        """See :meth:`app.clients.quota.UsageStore.add` (atomic increment)."""
        async with self._sessions.begin() as session:
            table = table_of(ApiUsageRow)
            insert = dialect_insert(session, table).values(
                **_key_values(key), requests_used=requests, reserved_manual=manual
            )
            await session.execute(
                insert.on_conflict_do_update(
                    index_elements=["usage_date", "provider", "sport"],
                    set_={
                        "requests_used": table.c.requests_used + insert.excluded.requests_used,
                        "reserved_manual": table.c.reserved_manual
                        + insert.excluded.reserved_manual,
                        "updated_at": utcnow(),
                    },
                )
            )
            return await _counts(session, key)

    async def raise_floor(self, key: UsageKey, requests_used: int) -> UsageCounts:
        """See :meth:`app.clients.quota.UsageStore.raise_floor` (never lowers the count)."""
        async with self._sessions.begin() as session:
            table = table_of(ApiUsageRow)
            insert = dialect_insert(session, table).values(
                **_key_values(key), requests_used=requests_used, reserved_manual=0
            )
            higher = case(
                (
                    table.c.requests_used < insert.excluded.requests_used,
                    insert.excluded.requests_used,
                ),
                else_=table.c.requests_used,
            )
            await session.execute(
                insert.on_conflict_do_update(
                    index_elements=["usage_date", "provider", "sport"],
                    set_={"requests_used": higher, "updated_at": utcnow()},
                )
            )
            return await _counts(session, key)


async def usage_on(session: AsyncSession, day: date) -> list[ApiUsageRecord]:
    """Every counter row of ``day`` (for ``/holat``), ordered by provider and sport."""
    rows = await session.scalars(
        select(ApiUsageRow)
        .where(ApiUsageRow.usage_date == day)
        .order_by(ApiUsageRow.provider, ApiUsageRow.sport)
    )
    return [
        ApiUsageRecord(
            UsageKey(row.usage_date, row.provider, row.sport),
            UsageCounts(row.requests_used, row.reserved_manual),
        )
        for row in rows
    ]


def _key_values(key: UsageKey) -> dict[str, object]:
    return {"usage_date": key.day, "provider": key.provider, "sport": key.sport}


async def _counts(session: AsyncSession, key: UsageKey) -> UsageCounts:
    row = (
        await session.execute(
            select(ApiUsageRow.requests_used, ApiUsageRow.reserved_manual).where(
                ApiUsageRow.usage_date == key.day,
                ApiUsageRow.provider == key.provider,
                ApiUsageRow.sport == key.sport,
            )
        )
    ).first()
    return UsageCounts() if row is None else UsageCounts(row[0], row[1])
