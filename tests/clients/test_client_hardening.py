"""T3 fix round: midnight reconcile, single-flight, body rate limit, fresh fetches, lanes."""

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx

from app.clients.api_football import (
    API_FOOTBALL_CONFIG,
    PROVIDER,
    QUOTA_SPORT,
    create_api_football_client,
)
from app.clients.http import ProviderUnavailableError
from app.clients.quota import (
    InMemoryUsageStore,
    QuotaGuard,
    QuotaPolicy,
    RequestPurpose,
    UsageCounts,
    UsageKey,
    shared_guard,
)
from app.clients.rate_limit import TokenBucket
from tests.clients.conftest import FakeTime, Harness, load

AUTO, MANUAL = RequestPurpose.AUTOMATIC, RequestPurpose.MANUAL
PARAMS = {"date": "2024-10-26"}
EMPTY = "fixtures_empty.json"
LIMIT_HEADERS = {"x-ratelimit-requests-limit": "100", "x-ratelimit-requests-remaining": "5"}


def usage_key(day: date) -> UsageKey:
    return UsageKey(day, PROVIDER)


# --------------------------------------------------------------------------- midnight


async def test_response_after_midnight_does_not_touch_the_new_day(
    harness: Harness, router: respx.MockRouter
) -> None:
    harness.time.now = datetime(2024, 10, 26, 23, 59, 59, tzinfo=UTC)

    def slow_response(_request: httpx.Request) -> httpx.Response:
        harness.time.now = harness.time.now + timedelta(seconds=2)  # answered at 00:00:01
        return httpx.Response(200, json=load(EMPTY), headers=LIMIT_HEADERS)

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
    router.get("/fixtures").respond(json=load(EMPTY), headers=LIMIT_HEADERS)
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
        return httpx.Response(200, json=load(EMPTY))

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
    route = router.get("/fixtures").respond(json=load(EMPTY))
    await harness.api.get_json("fixtures", PARAMS, purpose=AUTO, cache_ttl=0)
    await harness.api.get_json("fixtures", PARAMS, purpose=AUTO, cache_ttl=0)
    assert route.call_count == 2


# --------------------------------------------------------------------------- freshness


async def test_cache_ttl_zero_ignores_a_cached_body(
    harness: Harness, router: respx.MockRouter
) -> None:
    route = router.get("/fixtures").respond(json=load(EMPTY))
    await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)
    fresh = await harness.api.get_json("fixtures", PARAMS, purpose=AUTO, cache_ttl=0)
    assert not fresh.from_cache
    assert route.call_count == 2


async def test_cached_responses_keep_the_time_they_were_fetched(
    harness: Harness, router: respx.MockRouter
) -> None:
    router.get("/fixtures").respond(json=load(EMPTY))
    first = await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)
    harness.time.now += timedelta(minutes=5)
    harness.time.seconds += 300
    again = await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)
    assert again.from_cache
    assert again.fetched_at == first.fetched_at == datetime(2024, 10, 26, 8, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- body rate limit


async def test_rate_limit_inside_a_200_body_is_retried_with_backoff(
    harness: Harness, router: respx.MockRouter
) -> None:
    route = router.get("/fixtures").mock(
        side_effect=[
            httpx.Response(200, json={"errors": {"rateLimit": "Too many requests"}}),
            httpx.Response(200, json=load(EMPTY)),
        ]
    )
    response = await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)
    assert response.status_code == 200
    assert route.call_count == 2
    assert harness.sleep.delays == [0.5]
    assert (await harness.quota.status()).counts.requests_used == 2


async def test_persistent_body_rate_limit_ends_as_unavailable_and_is_not_cached(
    harness: Harness, router: respx.MockRouter
) -> None:
    route = router.get("/fixtures").respond(json={"errors": {"rateLimit": "Too many requests"}})
    with pytest.raises(ProviderUnavailableError):
        await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)
    assert route.call_count == API_FOOTBALL_CONFIG.max_attempts
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


async def test_clients_over_one_store_share_a_quota_guard() -> None:
    store = InMemoryUsageStore()
    first, first_http = create_api_football_client("k1", store, daily_limit=10)
    second, second_http = create_api_football_client("k1", store, daily_limit=10)
    other, other_http = create_api_football_client("k1", store, daily_limit=20)
    try:
        assert first.api.quota is second.api.quota
        assert other.api.quota is not first.api.quota
        assert (
            shared_guard(store, QuotaPolicy(daily_limit=10), provider=PROVIDER, sport=QUOTA_SPORT)
            is first.api.quota
        )
    finally:
        for http in (first_http, second_http, other_http):
            await http.aclose()


async def test_two_clients_cannot_overspend_the_daily_limit_together() -> None:
    store = InMemoryUsageStore()
    first, first_http = create_api_football_client("k1", store, daily_limit=10)
    second, second_http = create_api_football_client("k1", store, daily_limit=10)
    try:
        guards = [first.api.quota, second.api.quota] * 10
        outcomes = await asyncio.gather(
            *(guard.charge(MANUAL) for guard in guards), return_exceptions=True
        )
        assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 10
        assert (await first.api.quota.status()).counts.requests_used == 10
    finally:
        await first_http.aclose()
        await second_http.aclose()


def test_default_provider_config_flags_body_rate_limits() -> None:
    check = replace(API_FOOTBALL_CONFIG).retryable_body
    assert check is not None
    assert check({"errors": {"rateLimit": "x"}}) is not None
    assert check({"errors": {"token": "x"}}) is None
    assert check({"errors": []}) is None
    assert check([]) is None
