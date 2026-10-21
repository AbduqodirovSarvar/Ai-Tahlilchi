"""Daily API usage counters in the ``api_usage`` table.

:class:`SqlUsageStore` implements :class:`app.clients.quota.UsageStore`. Column meaning
(ADR 0007): ``requests_used`` = every request charged that UTC day; ``reserved_manual`` =
how many of them were manual (charged to the manual reserve). The reserve *size* is not
stored: it is policy (limit x share) and may change without rewriting history.

Each operation commits its own short transaction: a request that was sent must stay
counted even if the caller's unit of work later rolls back.

SQLite has one writer at a time per file. If the application holds an open write
transaction while a request is charged (it should not: commit before calling the network,
see contracts §3.1), the usage write cannot proceed. Instead of blocking the request path
for the whole ``busy_timeout`` or failing it, the store keeps the not-yet-stored delta in
memory, counts it in every read, and writes it together with the next operation that finds
the lock free (or on :meth:`SqlUsageStore.flush`). The application is a single process
(ADR 0002), so the in-memory view is authoritative meanwhile; only a crash in that window
loses the delta, and the provider's own counter (reconciled upwards from response headers)
repairs it. Build the store on an engine from :func:`app.db.engine.create_usage_engine`
so the wait for the lock is short.
"""

import asyncio
from dataclasses import dataclass
from datetime import date

from sqlalchemy import case, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.quota import UsageCounts, UsageKey
from app.db._sql import dialect_insert, table_of
from app.db.schema import ApiUsageRow, utcnow
from app.logging import get_logger

logger = get_logger(__name__)

_LOCK_MARKERS = ("locked", "busy")


@dataclass(frozen=True, slots=True)
class ApiUsageRecord:
    """One stored counter row."""

    key: UsageKey
    counts: UsageCounts


@dataclass(frozen=True, slots=True)
class _Delta:
    """Usage not yet stored: increments plus the highest provider-reported total."""

    requests: int = 0
    manual: int = 0
    floor: int = 0

    def then(self, other: "_Delta") -> "_Delta":
        """This delta followed by ``other``: ``max(max(x + a, f) + b, g)`` in one step."""
        return _Delta(
            self.requests + other.requests,
            self.manual + other.manual,
            max(self.floor + other.requests, other.floor),
        )

    def on_top_of(self, stored: UsageCounts) -> UsageCounts:
        return UsageCounts(
            max(stored.requests_used + self.requests, self.floor), stored.manual_used + self.manual
        )


class SqlUsageStore:
    """:class:`~app.clients.quota.UsageStore` persisted in ``api_usage``."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        """Use ``sessions`` to open one short transaction per operation."""
        self._sessions = sessions
        self._pending: dict[UsageKey, _Delta] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: UsageKey) -> UsageCounts:
        """See :meth:`app.clients.quota.UsageStore.get` (includes not yet stored usage)."""
        async with self._lock:
            stored = await self._read(key)
            return self._pending.get(key, _Delta()).on_top_of(stored)

    async def add(self, key: UsageKey, *, requests: int, manual: int) -> UsageCounts:
        """See :meth:`app.clients.quota.UsageStore.add` (atomic increment)."""
        return await self._write(key, _Delta(requests=requests, manual=manual))

    async def raise_floor(self, key: UsageKey, requests_used: int) -> UsageCounts:
        """See :meth:`app.clients.quota.UsageStore.raise_floor` (never lowers the count)."""
        return await self._write(key, _Delta(floor=requests_used))

    async def flush(self) -> int:
        """Store every delta still held in memory; returns how many days remain unstored."""
        async with self._lock:
            for key in list(self._pending):
                await self._persist(key, _Delta())
            return len(self._pending)

    async def _write(self, key: UsageKey, delta: _Delta) -> UsageCounts:
        async with self._lock:
            return await self._persist(key, delta)

    async def _persist(self, key: UsageKey, delta: _Delta) -> UsageCounts:
        total = self._pending.get(key, _Delta()).then(delta)
        try:
            async with self._sessions.begin() as session:
                await _apply(session, key, total)
                counts = await _counts(session, key)
        except OperationalError as exc:
            if not _lock_contention(exc):
                raise
            self._pending[key] = total
            logger.warning("api_usage_write_deferred", day=key.day.isoformat(), reason="db locked")
            return total.on_top_of(await self._read(key))
        self._pending.pop(key, None)
        return counts

    async def _read(self, key: UsageKey) -> UsageCounts:
        async with self._sessions() as session:
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


def _lock_contention(exc: OperationalError) -> bool:
    message = str(exc.orig).lower()
    return any(marker in message for marker in _LOCK_MARKERS)


def _key_values(key: UsageKey) -> dict[str, object]:
    return {"usage_date": key.day, "provider": key.provider, "sport": key.sport}


async def _apply(session: AsyncSession, key: UsageKey, delta: _Delta) -> None:
    """Add the increments and raise the total to the provider-reported floor, atomically."""
    table = table_of(ApiUsageRow)
    insert = dialect_insert(session, table).values(
        **_key_values(key),
        requests_used=max(delta.requests, delta.floor),
        reserved_manual=delta.manual,
    )
    incremented = table.c.requests_used + delta.requests
    await session.execute(
        insert.on_conflict_do_update(
            index_elements=["usage_date", "provider", "sport"],
            set_={
                "requests_used": case(
                    (incremented < delta.floor, delta.floor),
                    else_=incremented,
                ),
                "reserved_manual": table.c.reserved_manual + delta.manual,
                "updated_at": utcnow(),
            },
        )
    )


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
