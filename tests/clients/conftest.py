"""Client test helpers: recorded API-Football bodies, fake clocks, an offline ApiClient."""

import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import JsonValue

from app.clients.api_football import API_FOOTBALL_CONFIG, PROVIDER, ApiFootballClient
from app.clients.cache import TtlCache
from app.clients.http import ApiClient, ProviderConfig
from app.clients.quota import InMemoryUsageStore, QuotaGuard, QuotaPolicy
from app.clients.rate_limit import TokenBucket
from app.domain import CallRecorder

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "api_football"
BASE_URL = API_FOOTBALL_CONFIG.base_url
TEST_KEY = "test-api-key-0123456789"


def load(name: str) -> JsonValue:
    """A recorded/synthetic response body from ``tests/fixtures/api_football``."""
    body: JsonValue = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return body


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
    football: ApiFootballClient


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
    provider = config or replace(API_FOOTBALL_CONFIG, requests_per_minute=600, burst=100)
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
    return Harness(fake_time, sleep, store, quota, cache, api, ApiFootballClient(api))


@pytest.fixture
async def http_client(
    router: respx.MockRouter,  # noqa: ARG001 - requested so every request is mocked
) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=BASE_URL, headers={"x-apisports-key": TEST_KEY}
    ) as client:
        yield client


@pytest.fixture
def harness(fake_time: FakeTime, http_client: httpx.AsyncClient) -> Harness:
    return build_harness(fake_time, http_client)
