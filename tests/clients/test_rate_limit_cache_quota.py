"""Token bucket, TTL cache, quota policy/guard and cost estimates (no network)."""

from datetime import UTC, date, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.clients.cache import TtlCache, cache_key
from app.clients.cost import FetchPlan, estimate_requests
from app.clients.quota import (
    InMemoryUsageStore,
    QuotaExceededError,
    QuotaGuard,
    QuotaPolicy,
    RequestPurpose,
    UsageCounts,
    UsageKey,
)
from app.clients.rate_limit import TokenBucket
from tests.clients.conftest import FakeTime, RecordingSleep

AUTO, MANUAL = RequestPurpose.AUTOMATIC, RequestPurpose.MANUAL


# --------------------------------------------------------------------------- token bucket


async def test_bucket_allows_burst_then_spaces_requests(fake_time: FakeTime) -> None:
    sleep = RecordingSleep(fake_time)
    bucket = TokenBucket(10, capacity=2, clock=fake_time.monotonic, sleep=sleep)
    waits = [await bucket.acquire() for _ in range(4)]
    assert waits[:2] == [0.0, 0.0]
    assert waits[2:] == pytest.approx([6.0, 6.0])  # 10/min = one token every 6 s
    assert sleep.delays == pytest.approx([6.0, 6.0])


async def test_bucket_refills_with_elapsed_time(fake_time: FakeTime) -> None:
    sleep = RecordingSleep(fake_time)
    bucket = TokenBucket(60, capacity=1, clock=fake_time.monotonic, sleep=sleep)
    await bucket.acquire()
    fake_time.seconds += 5
    assert await bucket.acquire() == 0.0
    assert sleep.delays == []


async def test_bucket_survives_a_clock_that_does_not_move(fake_time: FakeTime) -> None:
    async def frozen_sleep(_seconds: float) -> None:
        return None

    bucket = TokenBucket(1, clock=fake_time.monotonic, sleep=frozen_sleep)
    await bucket.acquire()
    assert await bucket.acquire() == pytest.approx(60.0)
    assert await bucket.acquire() == pytest.approx(60.0)


@pytest.mark.parametrize(("rate", "capacity"), [(0, 1), (-1, 1), (10, 0)])
def test_bucket_rejects_bad_settings(rate: float, capacity: int) -> None:
    with pytest.raises(ValueError, match="must be"):
        TokenBucket(rate, capacity=capacity)


# --------------------------------------------------------------------------- cache


def test_cache_key_is_independent_of_parameter_order() -> None:
    assert cache_key("odds", {"b": 2, "a": "x"}) == cache_key("odds", {"a": "x", "b": 2})
    assert cache_key("odds", {}) == "odds?"


def test_cache_entries_expire_after_ttl(fake_time: FakeTime) -> None:
    cache: TtlCache[str] = TtlCache(clock=fake_time.monotonic)
    cache.set("k", "v", ttl=600)
    fake_time.seconds = 599.9
    assert cache.get("k") == "v"
    fake_time.seconds = 600
    assert cache.get("k") is None
    assert len(cache) == 0


def test_cache_six_hour_ttl_for_future_odds(fake_time: FakeTime) -> None:
    cache: TtlCache[str] = TtlCache(clock=fake_time.monotonic)
    cache.set("odds?fixture=1", "body", ttl=6 * 3600)
    fake_time.seconds = 5 * 3600
    assert cache.get("odds?fixture=1") == "body"
    fake_time.seconds = 6 * 3600 + 1
    assert cache.get("odds?fixture=1") is None


def test_cache_zero_ttl_stores_nothing_and_evicts(fake_time: FakeTime) -> None:
    cache: TtlCache[str] = TtlCache(clock=fake_time.monotonic)
    cache.set("k", "v", ttl=10)
    cache.set("k", "v2", ttl=0)
    assert cache.get("k") is None


def test_cache_is_bounded_lru(fake_time: FakeTime) -> None:
    cache: TtlCache[int] = TtlCache(clock=fake_time.monotonic, max_entries=2)
    cache.set("a", 1, ttl=10)
    cache.set("b", 2, ttl=10)
    assert cache.get("a") == 1  # "a" becomes most recent
    cache.set("c", 3, ttl=10)
    assert cache.get("b") is None
    assert cache.get("a") == 1
    cache.clear()
    assert len(cache) == 0
    with pytest.raises(ValueError, match="max_entries"):
        TtlCache(max_entries=0)


# --------------------------------------------------------------------------- quota policy


def test_reserve_is_thirty_percent_of_the_limit() -> None:
    assert QuotaPolicy(daily_limit=100).manual_reserve == 30


@pytest.mark.parametrize(
    ("used", "manual_used", "automatic", "manual"),
    [
        (0, 0, 70, 100),
        (69, 0, 1, 31),
        (70, 0, 0, 30),
        (80, 10, 0, 20),  # 20 reserve still unused by manual requests
        (60, 30, 40, 40),  # reserve spent by the user: the rest is shared
        (100, 30, 0, 0),
        (120, 0, 0, 0),  # provider-reported overuse never goes negative
    ],
)
def test_availability_per_purpose(used: int, manual_used: int, automatic: int, manual: int) -> None:
    policy = QuotaPolicy(daily_limit=100)
    counts = UsageCounts(used, manual_used)
    assert policy.available(counts, AUTO) == automatic
    assert policy.available(counts, MANUAL) == manual


@pytest.mark.parametrize(("limit", "share"), [(-1, 0.3), (100, -0.1), (100, 1.1)])
def test_policy_rejects_bad_settings(limit: int, share: float) -> None:
    with pytest.raises(ValueError, match="must be"):
        QuotaPolicy(daily_limit=limit, manual_reserve_share=share)


@given(
    used=st.integers(0, 150),
    manual_used=st.integers(0, 150),
    limit=st.integers(0, 200),
    share=st.floats(0, 1),
)
def test_automatic_never_exceeds_manual_availability(
    used: int, manual_used: int, limit: int, share: float
) -> None:
    policy = QuotaPolicy(daily_limit=limit, manual_reserve_share=share)
    counts = UsageCounts(used, min(manual_used, used))
    automatic = policy.available(counts, AUTO)
    manual = policy.available(counts, MANUAL)
    assert 0 <= automatic <= manual <= limit


# --------------------------------------------------------------------------- quota guard


def guard(fake_time: FakeTime, limit: int = 10) -> QuotaGuard:
    return QuotaGuard(
        InMemoryUsageStore(),
        QuotaPolicy(daily_limit=limit),
        provider="api-football",
        sport="football",
        clock=fake_time.utc,
    )


async def test_automatic_requests_stop_at_the_reserve(fake_time: FakeTime) -> None:
    quota = guard(fake_time)  # limit 10, reserve 3
    for _ in range(7):
        await quota.charge(AUTO)
    with pytest.raises(QuotaExceededError) as error:
        await quota.charge(AUTO)
    assert (error.value.needed, error.value.available) == (1, 0)
    assert error.value.purpose is AUTO
    status = await quota.status()
    assert status.counts == UsageCounts(7, 0)
    assert (status.remaining, status.reserve_remaining, status.automatic_available) == (3, 3, 0)


async def test_manual_requests_may_use_the_reserve_and_the_rest(fake_time: FakeTime) -> None:
    quota = guard(fake_time)
    status = await quota.charge(MANUAL, 10)
    assert status.counts == UsageCounts(10, 10)
    with pytest.raises(QuotaExceededError):
        await quota.charge(MANUAL)


async def test_refused_charge_counts_nothing(fake_time: FakeTime) -> None:
    quota = guard(fake_time)
    with pytest.raises(QuotaExceededError):
        await quota.charge(AUTO, 8)
    assert (await quota.status()).counts == UsageCounts()


async def test_ensure_available_checks_without_charging(fake_time: FakeTime) -> None:
    quota = guard(fake_time)
    await quota.ensure_available(7, AUTO)
    with pytest.raises(QuotaExceededError):
        await quota.ensure_available(8, AUTO)
    assert (await quota.status()).counts.requests_used == 0


async def test_counters_reset_on_a_new_utc_day(fake_time: FakeTime) -> None:
    quota = guard(fake_time)
    await quota.charge(MANUAL, 10)
    fake_time.now += timedelta(days=1)
    status = await quota.charge(AUTO)
    assert status.key == UsageKey(date(2024, 10, 27), "api-football", "football")
    assert status.counts == UsageCounts(1, 0)


async def test_day_key_uses_utc_even_for_offset_clocks(fake_time: FakeTime) -> None:
    fake_time.now = datetime(2024, 10, 27, 1, 0, tzinfo=UTC).astimezone(
        datetime(2024, 1, 1, tzinfo=UTC).astimezone().tzinfo
    )
    status = await guard(fake_time).status()
    assert status.key.day == date(2024, 10, 27)


async def test_reconcile_only_raises_the_count(fake_time: FakeTime) -> None:
    quota = guard(fake_time)
    await quota.charge(AUTO, 2)
    status = await quota.reconcile(provider_limit=10, provider_remaining=5)
    assert status.counts.requests_used == 5
    status = await quota.reconcile(provider_limit=10, provider_remaining=9)
    assert status.counts.requests_used == 5
    status = await quota.reconcile(provider_limit=10, provider_remaining=12)
    assert status.counts.requests_used == 5


# --------------------------------------------------------------------------- cost


def test_estimate_counts_every_endpoint() -> None:
    plan = FetchPlan(
        league_seasons=10,
        days=1,
        fixtures_by_id=41,
        odds_league_days=10,
        odds_pages_per_league_day=2,
        injury_fixtures=4,
        lineup_fixtures=3,
        statistics_fixtures=2,
        teams=1,
        odds_fixtures=1,
    )
    assert estimate_requests(plan) == 10 + 1 + 3 + 20 + 4 + 3 + 2 + 1 + 1
    assert estimate_requests(FetchPlan()) == 0
    assert estimate_requests(FetchPlan(fixtures_by_id=20)) == 1


def test_plan_rejects_negative_counts() -> None:
    with pytest.raises(ValueError, match="teams"):
        FetchPlan(teams=-1)
    with pytest.raises(ValueError, match="pages"):
        FetchPlan(odds_pages_per_league_day=0)
