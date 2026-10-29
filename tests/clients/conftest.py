"""Client test helpers: a neutral provider config, fake clocks and an offline ApiClient."""

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Final

import httpx
import pytest
import respx
from pydantic import JsonValue

from app.clients.cache import TtlCache
from app.clients.http import ApiClient, ProviderConfig
from app.clients.quota import InMemoryUsageStore, QuotaGuard, QuotaPolicy
from app.clients.rate_limit import TokenBucket
from app.domain import CallRecorder, Provider

PROVIDER: Final = "test-provider"
TEST_KEY: Final = "test-api-key-0123456789"
TEST_CONFIG: Final = ProviderConfig(
    name=PROVIDER,
    base_url="https://api.provider.test",
    auth_header="x-test-key",
    requests_per_minute=10,
    limit_header="x-ratelimit-requests-limit",
    remaining_header="x-ratelimit-requests-remaining",
    log_provider=Provider.FOOTBALL_DATA,
)
BASE_URL: Final = TEST_CONFIG.base_url
EMPTY_BODY: Final[JsonValue] = {"results": 0, "response": []}
"""A valid, empty provider body: what most transport tests answer with."""


@dataclass
class FakeTime:
    """Monotonic seconds and UTC wall clock that only move when told to."""

    seconds: float = 0.0
    now: datetime = datetime(2024, 10, 26, 8, 0, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.seconds

    def utc(self) -> datetime:
        return self.now


@dataclass
class RecordingSleep:
    """Async sleep that records requested delays and advances fake time instantly."""

    time: FakeTime
    delays: list[float] = field(default_factory=list)

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.time.seconds += seconds


@dataclass
class Harness:
    """Everything a client test needs, wired together without network or real waits."""

    time: FakeTime
    sleep: RecordingSleep
    store: InMemoryUsageStore
    quota: QuotaGuard
    cache: TtlCache[Any]
    api: ApiClient


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        yield mock


def build_harness(
    fake_time: FakeTime,
    http: httpx.AsyncClient,
    *,
    daily_limit: int = 100,
    config: ProviderConfig | None = None,
    recorder: CallRecorder | None = None,
) -> Harness:
    sleep = RecordingSleep(fake_time)
    store = InMemoryUsageStore()
    quota = QuotaGuard(
        store, QuotaPolicy(daily_limit=daily_limit), provider=PROVIDER, clock=fake_time.utc
    )
    cache: TtlCache[Any] = TtlCache(clock=fake_time.monotonic)
    provider = config or replace(TEST_CONFIG, requests_per_minute=600, burst=100)
    limiter = TokenBucket(
        provider.requests_per_minute,
        capacity=provider.burst,
        clock=fake_time.monotonic,
        sleep=sleep,
    )
    api = ApiClient(
        provider,
        http=http,
        quota=quota,
        limiter=limiter,
        cache=cache,
        sleep=sleep,
        jitter=lambda: 0.0,
        clock=fake_time.utc,
        recorder=recorder,
    )
    return Harness(fake_time, sleep, store, quota, cache, api)


@pytest.fixture
async def http_client(
    router: respx.MockRouter,  # noqa: ARG001 - requested so every request is mocked
) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=BASE_URL, headers={TEST_CONFIG.auth_header: TEST_KEY}
    ) as client:
        yield client


@pytest.fixture
def harness(fake_time: FakeTime, http_client: httpx.AsyncClient) -> Harness:
    return build_harness(fake_time, http_client)
