"""API usage counters while the application holds an open write transaction (SQLite)."""

import time
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.clients.quota import QuotaGuard, QuotaPolicy, RequestPurpose, UsageCounts, UsageKey
from app.db.api_usage import SqlUsageStore, usage_on
from app.db.engine import (
    USAGE_BUSY_TIMEOUT_MS,
    create_engine,
    create_session_factory,
    create_usage_engine,
)
from app.db.schema import LeagueRow
from app.domain import Sport

KEY = UsageKey(date(2024, 10, 26), "api-football", "football")
NOW = datetime(2024, 10, 26, 8, 0, tzinfo=UTC)
FAST_ENOUGH_SECONDS = 2.0


@pytest.fixture
async def usage_engine(
    engine: AsyncEngine,  # noqa: ARG001 - requested so the schema exists
    db_path: Path,
) -> AsyncIterator[AsyncEngine]:
    """The dedicated usage engine over the same file (``engine`` created the schema)."""
    usage = create_usage_engine(db_path)
    try:
        yield usage
    finally:
        await usage.dispose()


async def hold_write_lock(engine: AsyncEngine) -> AsyncSession:
    """An application session with an uncommitted write (the open-transaction scenario)."""
    session = create_session_factory(engine)()
    session.add(LeagueRow(sport=Sport.FOOTBALL, source="x", external_id="1", name="League"))
    await session.flush()
    return session


async def test_charge_does_not_wait_for_an_open_application_transaction(
    engine: AsyncEngine, usage_engine: AsyncEngine
) -> None:
    store = SqlUsageStore(create_session_factory(usage_engine))
    guard = QuotaGuard(
        store,
        QuotaPolicy(daily_limit=100),
        provider=KEY.provider,
        sport=KEY.sport,
        clock=lambda: NOW,
    )
    app_connection = await hold_write_lock(engine)
    try:
        started = time.monotonic()
        status = await guard.charge(RequestPurpose.AUTOMATIC, 3)
        elapsed = time.monotonic() - started
        assert elapsed < FAST_ENOUGH_SECONDS  # far below the 5 s application busy_timeout
        assert status.counts == UsageCounts(3, 0)
        # Reads and further charges keep counting what is not stored yet.
        assert (await guard.status()).counts == UsageCounts(3, 0)
        status = await guard.charge(RequestPurpose.MANUAL, 2)
        assert status.counts == UsageCounts(5, 2)
        assert (
            await guard.reconcile(status.key, provider_limit=100, provider_remaining=80)
        ).counts == UsageCounts(20, 2)
    finally:
        await app_connection.commit()
        await app_connection.close()
    # The next operation after the lock is gone stores everything that was deferred.
    assert (await guard.charge(RequestPurpose.AUTOMATIC)).counts == UsageCounts(21, 2)
    async with create_session_factory(usage_engine)() as session:
        (record,) = await usage_on(session, KEY.day)
    assert record.counts == UsageCounts(21, 2)


async def test_flush_stores_deferred_usage(engine: AsyncEngine, usage_engine: AsyncEngine) -> None:
    store = SqlUsageStore(create_session_factory(usage_engine))
    app_connection = await hold_write_lock(engine)
    try:
        await store.add(KEY, requests=4, manual=1)
        assert await store.flush() == 1  # still locked: one day remains unstored
    finally:
        await app_connection.rollback()
        await app_connection.close()
    assert await store.flush() == 0
    fresh = SqlUsageStore(create_session_factory(usage_engine))
    assert await fresh.get(KEY) == UsageCounts(4, 1)


async def test_other_database_errors_are_not_swallowed(tmp_path: Path) -> None:
    empty = create_usage_engine(tmp_path / "empty.db")  # no schema at all
    try:
        store = SqlUsageStore(create_session_factory(empty))
        with pytest.raises(OperationalError, match="no such table"):
            await store.add(KEY, requests=1, manual=0)
    finally:
        await empty.dispose()


async def test_usage_engine_uses_a_short_lock_wait(tmp_path: Path) -> None:
    usage = create_usage_engine(tmp_path / "u.db")
    regular = create_engine(tmp_path / "r.db")
    try:
        async with usage.connect() as connection:
            wait = (await connection.execute(text("PRAGMA busy_timeout"))).scalar_one()
        assert wait == USAGE_BUSY_TIMEOUT_MS
        async with regular.connect() as connection:
            assert (await connection.execute(text("PRAGMA busy_timeout"))).scalar_one() == 5000
    finally:
        await usage.dispose()
        await regular.dispose()
