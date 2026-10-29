"""T3 fix round: midnight reconcile, single-flight, body rate limit, fresh fetches, lanes."""

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx
from pydantic import JsonValue

from app.clients.http import ProviderUnavailableError
from app.clients.quota import (
    InMemoryUsageStore,
    QuotaGuard,
    QuotaPolicy,
    RequestPurpose,
    UsageCounts,
    UsageKey,
)
from app.clients.rate_limit import TokenBucket
from tests.clients.conftest import (
    EMPTY_BODY,
    PROVIDER,
    TEST_CONFIG,
    FakeTime,
    Harness,
    build_harness,
)

AUTO, MANUAL = RequestPurpose.AUTOMATIC, RequestPurpose.MANUAL
PARAMS = {"date": "2024-10-26"}
LIMIT_HEADERS = {"x-ratelimit-requests-limit": "100", "x-ratelimit-requests-remaining": "5"}


def usage_key(day: date) -> UsageKey:
    return UsageKey(day, PROVIDER)


def rate_limited_body(payload: JsonValue) -> str | None:
    """A provider that reports its per-minute limit inside an HTTP 200 body."""
    if isinstance(payload, dict) and isinstance(payload.get("errors"), dict):
        return "rate limit reported in body"
    return None


@pytest.fixture
def limited(fake_time: FakeTime, http_client: httpx.AsyncClient) -> Harness:
    config = replace(
        TEST_CONFIG, requests_per_minute=600, burst=100, retryable_body=rate_limited_body
    )
    return build_harness(fake_time, http_client, config=config)


# --------------------------------------------------------------------------- midnight


async def test_response_after_midnight_does_not_touch_the_new_day(
    harness: Harness, router: respx.MockRouter
) -> None:
    harness.time.now = datetime(2024, 10, 26, 23, 59, 59, tzinfo=UTC)

    def slow_response(_request: httpx.Request) -> httpx.Response:
        harness.time.now = harness.time.now + timedelta(seconds=2)  # answered at 00:00:01
        return httpx.Response(200, json=EMPTY_BODY, headers=LIMIT_HEADERS)

    router.get("/fixtures").mock(side_effect=slow_response)
    await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)
    assert harness.time.now.date() == date(2024, 10, 27)
    # The provider's "95 used" describes the day the request was charged on...
    assert await harness.store.get(usage_key(date(2024, 10, 26))) == UsageCounts(1, 0)
    # ...and must neither raise the new day's floor nor eat its manual reserve.
    status = await harness.quota.status()
    assert status.counts == UsageCounts(0, 0)
    assert status.automatic_available == 70
    assert status.reserve_remaining == 30


async def test_reconcile_on_the_same_day_still_raises_the_floor(
    harness: Harness, router: respx.MockRouter
) -> None:
    router.get("/fixtures").respond(json=EMPTY_BODY, headers=LIMIT_HEADERS)
    await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)
    assert (await harness.quota.status()).counts.requests_used == 95


async def test_reconcile_never_exceeds_the_daily_limit(fake_time: FakeTime) -> None:
    quota = QuotaGuard(
        InMemoryUsageStore(), QuotaPolicy(daily_limit=100), provider=PROVIDER, clock=fake_time.utc
    )
    key = (await quota.charge(AUTO)).key
    status = await quota.reconcile(key, provider_limit=500, provider_remaining=-50)
    assert status.counts.requests_used == 100
    assert status.remaining == 0


# --------------------------------------------------------------------------- single flight


async def test_concurrent_identical_requests_share_a_failure(
    harness: Harness, router: respx.MockRouter
) -> None:
    async def failing(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01)
        return httpx.Response(401)

    route = router.get("/fixtures").mock(side_effect=failing)
    results = await asyncio.gather(
        *(harness.api.get_json("fixtures", PARAMS, purpose=AUTO) for _ in range(5)),
        return_exceptions=True,
    )
    assert route.call_count == 1
    assert all(isinstance(result, Exception) for result in results)
    assert (await harness.quota.status()).counts.requests_used == 1


async def test_cancelling_one_waiter_does_not_cancel_the_shared_request(
    harness: Harness, router: respx.MockRouter
) -> None:
    async def slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.02)
        return httpx.Response(200, json=EMPTY_BODY)

    route = router.get("/fixtures").mock(side_effect=slow)
    first = asyncio.create_task(harness.api.get_json("fixtures", PARAMS, purpose=AUTO))
    second = asyncio.create_task(harness.api.get_json("fixtures", PARAMS, purpose=MANUAL))
    await asyncio.sleep(0)
    first.cancel()
    response = await second
    assert response.status_code == 200
    assert route.call_count == 1
    # A later call is answered from the cache the shared request filled.
    assert (await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)).from_cache


async def test_a_new_request_after_completion_is_not_joined(
    harness: Harness, router: respx.MockRouter
) -> None:
    route = router.get("/fixtures").respond(json=EMPTY_BODY)
    await harness.api.get_json("fixtures", PARAMS, purpose=AUTO, cache_ttl=0)
    await harness.api.get_json("fixtures", PARAMS, purpose=AUTO, cache_ttl=0)
    assert route.call_count == 2


# --------------------------------------------------------------------------- freshness


async def test_cache_ttl_zero_ignores_a_cached_body(
    harness: Harness, router: respx.MockRouter
) -> None:
    route = router.get("/fixtures").respond(json=EMPTY_BODY)
    await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)
    fresh = await harness.api.get_json("fixtures", PARAMS, purpose=AUTO, cache_ttl=0)
    assert not fresh.from_cache
    assert route.call_count == 2


async def test_cached_responses_keep_the_time_they_were_fetched(
    harness: Harness, router: respx.MockRouter
) -> None:
    router.get("/fixtures").respond(json=EMPTY_BODY)
    first = await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)
    harness.time.now += timedelta(minutes=5)
    harness.time.seconds += 300
    again = await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)
    assert again.from_cache
    assert again.fetched_at == first.fetched_at == datetime(2024, 10, 26, 8, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- body rate limit


async def test_rate_limit_inside_a_200_body_is_retried_with_backoff(
    limited: Harness, router: respx.MockRouter
) -> None:
    harness = limited
    route = router.get("/fixtures").mock(
        side_effect=[
            httpx.Response(200, json={"errors": {"rateLimit": "Too many requests"}}),
            httpx.Response(200, json=EMPTY_BODY),
        ]
    )
    response = await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)
    assert response.status_code == 200
    assert route.call_count == 2
    assert harness.sleep.delays == [0.5]
    assert (await harness.quota.status()).counts.requests_used == 2


async def test_persistent_body_rate_limit_ends_as_unavailable_and_is_not_cached(
    limited: Harness, router: respx.MockRouter
) -> None:
    harness = limited
    route = router.get("/fixtures").respond(json={"errors": {"rateLimit": "Too many requests"}})
    with pytest.raises(ProviderUnavailableError):
        await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)
    assert route.call_count == TEST_CONFIG.max_attempts
    assert not harness.api.cached("fixtures", PARAMS)


# --------------------------------------------------------------------------- priority lanes


async def test_manual_requests_go_before_queued_automatic_ones(fake_time: FakeTime) -> None:
    gate = asyncio.Event()
    waiting = 0

    async def gated_sleep(seconds: float) -> None:
        nonlocal waiting
        waiting += 1
        await gate.wait()
        fake_time.seconds += seconds

    bucket = TokenBucket(60, capacity=1, clock=fake_time.monotonic, sleep=gated_sleep)
    await bucket.acquire()  # empties the bucket
    order: list[str] = []

    async def take(name: str, *, urgent: bool) -> None:
        await bucket.acquire(urgent=urgent)
        order.append(name)

    tasks = []
    for name, urgent in (("auto-1", False), ("auto-2", False), ("auto-3", False)):
        tasks.append(asyncio.create_task(take(name, urgent=urgent)))
        await asyncio.sleep(0)
    tasks.append(asyncio.create_task(take("manual", urgent=True)))
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(*tasks)
    # auto-1 already held the bucket; the user's request jumps the rest of the queue.
    assert order == ["auto-1", "manual", "auto-2", "auto-3"]


async def test_a_cancelled_waiter_does_not_block_the_queue(fake_time: FakeTime) -> None:
    gate = asyncio.Event()

    async def gated_sleep(seconds: float) -> None:
        await gate.wait()
        fake_time.seconds += seconds

    bucket = TokenBucket(60, capacity=1, clock=fake_time.monotonic, sleep=gated_sleep)
    await bucket.acquire()
    holder = asyncio.create_task(bucket.acquire())
    await asyncio.sleep(0)
    doomed = asyncio.create_task(bucket.acquire(urgent=True))
    survivor = asyncio.create_task(bucket.acquire())
    await asyncio.sleep(0)
    doomed.cancel()
    gate.set()
    await asyncio.gather(holder, survivor)
    assert doomed.cancelled()


# --------------------------------------------------------------------------- shared guard
