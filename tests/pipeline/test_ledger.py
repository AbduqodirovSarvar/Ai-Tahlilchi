"""Idempotency keys, stamps, run status and the error list."""

import asyncio
from datetime import UTC, date, datetime, timedelta

from app.pipeline.ledger import (
    DEFAULT_LEASE,
    KEY_RETENTION,
    MAX_ATTEMPTS,
    MAX_KEPT_ERRORS,
    RETRY_CAP,
    Ledger,
    LedgerKey,
    backoff,
)

NOW = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)
KEY = LedgerKey("daily", date(2026, 10, 21), 7, "digest")


async def test_a_key_is_claimed_once(ledger: Ledger) -> None:
    assert await ledger.claim(KEY, now=NOW)
    assert not await ledger.claim(KEY, now=NOW + timedelta(minutes=1))
    assert str(KEY) == "daily/2026-10-21/7/digest"


async def test_concurrent_claims_have_one_winner(ledger: Ledger) -> None:
    results = await asyncio.gather(*(ledger.claim(KEY, now=NOW) for _ in range(5)))
    assert results.count(True) == 1


async def test_an_abandoned_claim_is_retaken_after_the_lease(ledger: Ledger) -> None:
    await ledger.claim(KEY, now=NOW)
    assert not await ledger.claim(KEY, now=NOW + DEFAULT_LEASE - timedelta(seconds=1))
    assert await ledger.claim(KEY, now=NOW + DEFAULT_LEASE)


async def test_a_done_key_is_never_given_out_again(ledger: Ledger) -> None:
    await ledger.claim(KEY, now=NOW)
    await ledger.complete(KEY, now=NOW, detail="signals=3")
    assert await ledger.is_done(KEY)
    assert not await ledger.claim(KEY, now=NOW + timedelta(days=2))
    entry = await ledger.get(KEY)
    assert entry is not None
    assert entry.ok
    assert entry.detail == "signals=3"


async def test_a_failed_key_is_retried_after_its_backoff(ledger: Ledger) -> None:
    await ledger.claim(KEY, now=NOW)
    failure = await ledger.fail(KEY, now=NOW, detail="boom")
    entry = await ledger.get(KEY)
    assert entry is not None
    assert not entry.ok
    assert failure.retry_after == NOW + backoff(1) == entry.retry_after
    assert not await ledger.claim(KEY, now=NOW)
    assert not await ledger.claim(KEY, now=NOW + backoff(1) - timedelta(seconds=1))
    assert await ledger.claim(KEY, now=NOW + backoff(1))
    assert not await ledger.claim(KEY, now=NOW + backoff(1))


async def test_backoff_doubles_up_to_a_cap_and_attempts_are_limited(ledger: Ledger) -> None:
    waits = [backoff(n) for n in range(1, 12)]
    assert waits[:3] == [timedelta(minutes=1), timedelta(minutes=2), timedelta(minutes=4)]
    assert max(waits) == RETRY_CAP
    now = NOW
    for attempt in range(1, MAX_ATTEMPTS + 1):
        assert await ledger.claim(KEY, now=now)
        failure = await ledger.fail(KEY, now=now)
        assert failure.attempts == attempt
        assert failure.exhausted is (attempt == MAX_ATTEMPTS)
        now += timedelta(hours=1)
    entry = await ledger.get(KEY)
    assert entry is not None
    assert entry.status == "exhausted"
    assert not entry.ok
    assert not await ledger.claim(KEY, now=now + timedelta(days=30))


async def test_completing_after_failures_clears_the_retry(ledger: Ledger) -> None:
    await ledger.claim(KEY, now=NOW)
    await ledger.fail(KEY, now=NOW)
    await ledger.claim(KEY, now=NOW + timedelta(hours=1))
    await ledger.complete(KEY, now=NOW + timedelta(hours=1))
    entry = await ledger.get(KEY)
    assert entry is not None
    assert entry.retry_after is None
    assert entry.ok


async def test_due_retries_lists_failed_keys_whose_backoff_is_over(ledger: Ledger) -> None:
    other = LedgerKey("daily", date(2026, 10, 21), 8, "digest")
    await ledger.claim(KEY, now=NOW)
    await ledger.fail(KEY, now=NOW)
    await ledger.claim(other, now=NOW)
    await ledger.complete(other, now=NOW)
    assert await ledger.due_retries("daily", now=NOW) == []
    assert await ledger.due_retries("daily", now=NOW + backoff(1)) == [KEY]
    assert await ledger.due_retries("settled", now=NOW + backoff(1)) == []


async def test_racing_takeovers_of_a_failed_key_have_one_winner(ledger: Ledger) -> None:
    await ledger.claim(KEY, now=NOW)
    await ledger.fail(KEY, now=NOW)
    later = NOW + timedelta(hours=1)
    results = await asyncio.gather(*(ledger.claim(KEY, now=later) for _ in range(8)))
    assert results.count(True) == 1


async def test_expired_keys_are_purged_and_fresh_ones_kept(ledger: Ledger) -> None:
    old_day = LedgerKey("daily", (NOW - KEY_RETENTION - timedelta(days=1)).date(), 1, "digest")
    await ledger.complete(old_day, now=NOW - KEY_RETENTION - timedelta(days=1))
    await ledger.complete(KEY, now=NOW)
    await ledger.stamp("query", "q1", now=NOW - KEY_RETENTION - timedelta(days=1))
    await ledger.stamp("query", "q2", now=NOW)
    await ledger.stamp("cover.fixtures", "football:x", now=NOW - KEY_RETENTION * 2)
    assert await ledger.purge_expired(now=NOW) == 2
    assert await ledger.get(old_day) is None
    assert await ledger.is_done(KEY)
    assert await ledger.stamped_at("query", "q1") is None
    assert await ledger.stamped_at("query", "q2") == NOW
    assert await ledger.stamped_at("cover.fixtures", "football:x") is not None


async def test_release_forgets_an_unused_claim_but_not_a_done_key(ledger: Ledger) -> None:
    await ledger.claim(KEY, now=NOW)
    await ledger.release(KEY)
    assert await ledger.get(KEY) is None
    await ledger.complete(KEY, now=NOW)
    await ledger.release(KEY)
    assert await ledger.is_done(KEY)


async def test_stamps_keep_the_latest_time(ledger: Ledger) -> None:
    assert await ledger.stamped_at("cover", "football:2026-10-21") is None
    await ledger.stamp("cover", "football:2026-10-21", now=NOW)
    await ledger.stamp("cover", "football:2026-10-21", now=NOW + timedelta(hours=1))
    assert await ledger.stamped_at("cover", "football:2026-10-21") == NOW + timedelta(hours=1)


async def test_run_status_per_job(ledger: Ledger) -> None:
    await ledger.record_run("refresh", ok=True, detail="fixtures=3", now=NOW)
    await ledger.record_run("settle", ok=False, detail="boom", now=NOW)
    await ledger.record_run("refresh", ok=False, detail="again", now=NOW + timedelta(hours=1))
    runs = await ledger.last_runs()
    assert set(runs) == {"refresh", "settle"}
    assert not runs["refresh"].ok
    assert runs["refresh"].done_at == NOW + timedelta(hours=1)


async def test_only_the_newest_errors_are_kept(ledger: Ledger) -> None:
    for number in range(MAX_KEPT_ERRORS + 5):
        await ledger.record_error("refresh", f"error {number}", now=NOW + timedelta(seconds=number))
    errors = await ledger.recent_errors(limit=3)
    assert [message for _, _, message in errors] == [
        f"error {MAX_KEPT_ERRORS + 4}",
        f"error {MAX_KEPT_ERRORS + 3}",
        f"error {MAX_KEPT_ERRORS + 2}",
    ]
    assert len(await ledger.recent_errors(limit=1000)) == MAX_KEPT_ERRORS
    assert errors[0][1] == "refresh"
