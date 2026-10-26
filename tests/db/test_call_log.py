"""Call-log repository and the SQL recorder: paging, filters, retention, secrecy, safety."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from app.db.call_log import MAX_LOGGED_ERROR, SqlCallRecorder
from app.db.engine import create_session_factory
from app.db.repo import (
    REQUEST_PREVIEW_CHARS,
    call_stats,
    count_calls,
    get_call,
    list_calls,
    purge_calls_older_than,
    record_call,
)
from app.db.schema import ProviderCallRow
from app.domain import MAX_LOGGED_TEXT, CallOutcome, CallRecord, Provider
from app.pipeline.redaction import register_secrets, scrub_text

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
SECRET = "sk-live-0123456789abcdef"


def call(minutes: int = 0, **changes: object) -> CallRecord:
    values: dict[str, object] = {
        "at": NOW + timedelta(minutes=minutes),
        "provider": Provider.API_FOOTBALL,
        "operation": "fixtures",
        "outcome": CallOutcome.OK,
        "duration_ms": 120,
        "request": '{"date": "2026-10-26"}',
        "response": '{"response": []}',
        "http_status": 200,
        "quota_cost": 1,
        "size_bytes": 16,
    }
    values.update(changes)
    return CallRecord(**values)  # type: ignore[arg-type]  # test helper builds from a dict


def keep_all(text: str) -> str:
    return text


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


async def test_a_call_round_trips_with_full_text(db_session: AsyncSession) -> None:
    call_id = await record_call(db_session, call(response="x" * 5000))

    detail = await get_call(db_session, call_id)

    assert detail is not None
    assert (detail.provider, detail.operation, detail.outcome) == (
        Provider.API_FOOTBALL,
        "fixtures",
        CallOutcome.OK,
    )
    assert detail.at == NOW
    assert detail.response == "x" * 5000
    assert detail.request == '{"date": "2026-10-26"}'
    assert (detail.http_status, detail.quota_cost, detail.size_bytes) == (200, 1, 16)
    assert await get_call(db_session, call_id + 1) is None


async def test_list_is_newest_first_pages_and_leaves_the_response_out(
    db_session: AsyncSession,
) -> None:
    for minute in range(5):
        await record_call(
            db_session, call(minute, request="r" * 1000, response=f"answer {minute}" * 100)
        )

    first = await list_calls(db_session, limit=2, offset=0)
    second = await list_calls(db_session, limit=2, offset=2)
    last = await list_calls(db_session, limit=2, offset=4)

    assert [c.at for c in first] == [NOW + timedelta(minutes=4), NOW + timedelta(minutes=3)]
    assert [c.at for c in second] == [NOW + timedelta(minutes=2), NOW + timedelta(minutes=1)]
    assert [c.at for c in last] == [NOW]
    assert await list_calls(db_session, limit=2, offset=10) == []
    assert all(len(c.request_preview) == REQUEST_PREVIEW_CHARS for c in first)
    assert not hasattr(first[0], "response")
    assert await count_calls(db_session) == 5


async def test_equal_timestamps_order_by_id_so_pages_never_overlap(
    db_session: AsyncSession,
) -> None:
    ids = [await record_call(db_session, call(0)) for _ in range(4)]

    page = [c.id for c in await list_calls(db_session, limit=4, offset=0)]

    assert page == sorted(ids, reverse=True)


async def test_filters_by_provider_and_outcome(db_session: AsyncSession) -> None:
    await record_call(db_session, call(0))
    await record_call(db_session, call(1, outcome=CallOutcome.ERROR, error="HTTP 500"))
    await record_call(db_session, call(2, provider=Provider.GEMINI, operation="Review"))
    await record_call(
        db_session, call(3, provider=Provider.GEMINI, outcome=CallOutcome.ERROR, error="x")
    )

    gemini = await list_calls(db_session, provider=Provider.GEMINI, limit=10, offset=0)
    errors = await list_calls(db_session, outcome=CallOutcome.ERROR, limit=10, offset=0)
    both = await list_calls(
        db_session, provider=Provider.API_FOOTBALL, outcome=CallOutcome.ERROR, limit=10, offset=0
    )

    assert [c.provider for c in gemini] == [Provider.GEMINI, Provider.GEMINI]
    assert len(errors) == 2
    assert [c.error for c in both] == ["HTTP 500"]
    assert await count_calls(db_session, provider=Provider.GEMINI) == 2
    assert await count_calls(db_session, outcome=CallOutcome.ERROR) == 2
    assert await count_calls(db_session, provider=Provider.GEMINI, outcome=CallOutcome.OK) == 1
    assert await count_calls(db_session, outcome=CallOutcome.CACHED) == 0


async def test_purge_deletes_only_older_calls_and_honours_the_limit(
    db_session: AsyncSession,
) -> None:
    for day in range(5):
        await record_call(db_session, call(-60 * 24 * day))
    cutoff = NOW - timedelta(days=1, hours=1)  # days 2, 3 and 4 are older

    first = await purge_calls_older_than(db_session, cutoff, limit=2)
    second = await purge_calls_older_than(db_session, cutoff, limit=2)
    third = await purge_calls_older_than(db_session, cutoff, limit=2)

    assert (first, second, third) == (2, 1, 0)
    assert await count_calls(db_session) == 2


async def test_purge_without_limit_and_on_an_empty_table(db_session: AsyncSession) -> None:
    assert await purge_calls_older_than(db_session, NOW) == 0
    await record_call(db_session, call(-10))
    await record_call(db_session, call(10))

    assert await purge_calls_older_than(db_session, NOW) == 1
    assert await purge_calls_older_than(db_session, NOW) == 0


async def test_stats_group_by_operation_and_ignore_cache_in_the_average(
    db_session: AsyncSession,
) -> None:
    await record_call(db_session, call(0, duration_ms=100))
    await record_call(db_session, call(1, duration_ms=300, outcome=CallOutcome.ERROR))
    await record_call(db_session, call(2, duration_ms=0, outcome=CallOutcome.CACHED, quota_cost=0))
    await record_call(
        db_session, call(3, duration_ms=0, outcome=CallOutcome.QUOTA_BLOCKED, quota_cost=0)
    )
    await record_call(db_session, call(4, operation="odds", quota_cost=1))
    await record_call(db_session, call(-60 * 48, operation="odds"))  # before `since`

    stats = await call_stats(db_session, NOW - timedelta(hours=1))

    by_operation = {s.operation: s for s in stats}
    assert set(by_operation) == {"fixtures", "odds"}
    fixtures = by_operation["fixtures"]
    assert (fixtures.calls, fixtures.errors, fixtures.cached, fixtures.blocked) == (4, 1, 1, 1)
    assert fixtures.avg_duration_ms == pytest.approx(200.0)
    assert fixtures.quota_cost == 2
    assert (by_operation["odds"].calls, by_operation["odds"].quota_cost) == (1, 1)


async def test_stats_average_is_none_when_nothing_reached_the_provider(
    db_session: AsyncSession,
) -> None:
    await record_call(db_session, call(0, outcome=CallOutcome.CACHED, quota_cost=0))

    (stat,) = await call_stats(db_session, NOW - timedelta(days=1))

    assert stat.avg_duration_ms is None
    assert await call_stats(db_session, NOW + timedelta(days=1)) == []


# ------------------------------------------------------------------ the recorder


async def test_recorder_stores_a_call_in_its_own_transaction(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    recorder = SqlCallRecorder(sessions, scrub=keep_all)

    await recorder.record(call(0))

    async with sessions() as session:
        assert await count_calls(session) == 1


async def test_recorder_scrubs_before_it_cuts_and_stores_no_secret(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    register_secrets([SECRET])
    try:
        recorder = SqlCallRecorder(sessions, scrub=lambda text: scrub_text(text, limit=len(text)))
        padding = "a" * (MAX_LOGGED_TEXT - 5)  # the secret straddles the cut-off
        await recorder.record(
            call(
                request=f"{padding}{SECRET} key={SECRET}",
                response=f'{{"echo": "{SECRET}", "bearer": "Bearer abcdefghijklmnop"}}',
                error=f"GET https://x/?apikey={SECRET} failed",
            )
        )
    finally:
        register_secrets(())

    async with sessions() as session:
        stored = (await session.execute(select(ProviderCallRow))).scalar_one()
    for text in (stored.request, stored.response, stored.error):
        assert text is not None
        assert SECRET not in text
        assert "abcdefghijklmnop" not in text
    assert stored.request.startswith("a" * 100)
    assert stored.response is not None
    assert "***" in stored.response


async def test_recorder_cuts_long_texts_with_a_marker(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    recorder = SqlCallRecorder(sessions, scrub=keep_all)

    await recorder.record(
        call(request="q" * (MAX_LOGGED_TEXT + 50), response="r" * 30_000, error="e" * 5000)
    )

    async with sessions() as session:
        stored = (await session.execute(select(ProviderCallRow))).scalar_one()
    assert stored.request.startswith("q" * MAX_LOGGED_TEXT)
    assert stored.request.endswith("[50 ta belgi qirqildi]")
    assert stored.response is not None
    assert len(stored.response) < 30_000
    assert stored.error is not None
    assert len(stored.error) < 5000
    assert stored.error.startswith("e" * MAX_LOGGED_ERROR)


async def test_a_storage_failure_never_reaches_the_caller_and_is_logged_without_the_text(
    engine: AsyncEngine,
) -> None:
    # No table: the insert fails as it would on a locked or broken database.
    async with engine.begin() as connection:
        await connection.exec_driver_sql("DROP TABLE provider_calls")
    recorder = SqlCallRecorder(create_session_factory(engine), scrub=keep_all)

    with capture_logs() as logs:
        await recorder.record(call(request="SECRET-PROMPT-TEXT"))

    (entry,) = [log for log in logs if log["event"] == "call_log_write_failed"]
    assert entry["error_type"] == "OperationalError"
    assert (entry["provider"], entry["operation"], entry["outcome"]) == (
        "api_football",
        "fixtures",
        "ok",
    )
    assert "SECRET-PROMPT-TEXT" not in repr(logs)


async def test_a_failing_scrubber_does_not_break_the_caller(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    def broken(_text: str) -> str:
        message = "scrubber broke"
        raise ValueError(message)

    await SqlCallRecorder(sessions, scrub=broken).record(call())

    async with sessions() as session:
        assert await count_calls(session) == 0


async def test_a_locked_database_drops_the_row_instead_of_raising(
    engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession]
) -> None:
    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def fail(*_args: object) -> None:
        message = "database is locked"
        raise sqlite3.OperationalError(message)

    recorder = SqlCallRecorder(sessions, scrub=keep_all)
    await recorder.record(call())  # must not raise

    event.remove(engine.sync_engine, "before_cursor_execute", fail)
    async with sessions() as session:
        assert await count_calls(session) == 0
