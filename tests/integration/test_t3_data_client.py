"""T3 QA: provider client risks (quota under concurrency, retries, errors, secrets).

Risk-based tests written independently of the unit tests in ``tests/clients``: they attack the
properties whose failure costs real money or the whole daily API budget (plan §9).
"""

import asyncio
import json
import math
from datetime import date

import httpx
import pytest
import respx
import structlog
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncEngine

from app.clients import api_football as api_football_module
from app.clients import http as http_module
from app.clients.api_football import ApiFootballError, validate_records
from app.clients.api_football_models import Envelope, FixtureItem
from app.clients.http import ProviderHttpError, ProviderUnavailableError, parse_retry_after
from app.clients.quota import (
    InMemoryUsageStore,
    QuotaExceededError,
    QuotaGuard,
    QuotaPolicy,
    QuotaStatus,
    RequestPurpose,
    UsageStore,
)
from app.db.api_usage import SqlUsageStore
from app.db.engine import create_session_factory
from app.errors import ExternalServiceError
from tests.clients.conftest import TEST_KEY, FakeTime, Harness, build_harness, load

AUTO, MANUAL = RequestPurpose.AUTOMATIC, RequestPurpose.MANUAL
LIMIT, RESERVE = 100, 30
EMPTY = "fixtures_empty.json"


def day_params(index: int) -> dict[str, str]:
    """Distinct query parameters per request so the cache never answers."""
    return {"date": f"2030-01-{index % 28 + 1:02d}", "page": str(index)}


# --------------------------------------------------------------------------- quota


def make_guard(store: UsageStore, fake_time: FakeTime) -> QuotaGuard:
    return QuotaGuard(
        store, QuotaPolicy(daily_limit=LIMIT), provider="api-football", clock=fake_time.utc
    )


async def charge_many(
    guard: QuotaGuard, purposes: list[RequestPurpose]
) -> list[QuotaStatus | None]:
    async def one(purpose: RequestPurpose) -> QuotaStatus | None:
        try:
            return await guard.charge(purpose)
        except QuotaExceededError:
            return None

    return list(await asyncio.gather(*(one(purpose) for purpose in purposes)))


async def test_concurrent_automatic_charges_never_enter_the_reserve(fake_time: FakeTime) -> None:
    guard = make_guard(InMemoryUsageStore(), fake_time)
    results = await charge_many(guard, [AUTO] * 400)
    assert sum(result is not None for result in results) == LIMIT - RESERVE
    status = await guard.status()
    assert (status.counts.requests_used, status.counts.manual_used) == (70, 0)
    assert (status.automatic_available, status.reserve_remaining) == (0, RESERVE)


async def test_manual_can_still_use_the_whole_reserve_after_automatic_exhaustion(
    fake_time: FakeTime,
) -> None:
    guard = make_guard(InMemoryUsageStore(), fake_time)
    await charge_many(guard, [AUTO] * 150)
    results = await charge_many(guard, [MANUAL] * 80)
    assert sum(result is not None for result in results) == RESERVE
    counts = (await guard.status()).counts
    assert (counts.requests_used, counts.manual_used) == (LIMIT, RESERVE)


async def test_interleaved_purposes_keep_the_reserve_invariant(fake_time: FakeTime) -> None:
    """After every successful automatic charge the manual reserve is still intact.

    ``requests_used <= limit - max(reserve - manual_used, 0)`` is exactly "automatic never
    dipped into the part of the reserve manual requests have not used yet"; every
    ``QuotaStatus`` returned by ``charge`` is an atomic snapshot taken under the lock.
    """
    guard = make_guard(InMemoryUsageStore(), fake_time)
    purposes = [AUTO, AUTO, MANUAL] * 60  # 120 automatic, 60 manual, 180 attempts
    results = await charge_many(guard, purposes)
    for purpose, status in zip(purposes, results, strict=True):
        if purpose is AUTO and status is not None:
            counts = status.counts
            assert counts.requests_used <= LIMIT - max(RESERVE - counts.manual_used, 0)
    final = (await guard.status()).counts
    assert final.requests_used <= LIMIT
    assert final.manual_used <= final.requests_used


async def test_sql_store_keeps_the_reserve_under_concurrency(
    engine: AsyncEngine, fake_time: FakeTime
) -> None:
    store = SqlUsageStore(create_session_factory(engine))
    guard = make_guard(store, fake_time)
    results = await charge_many(guard, [AUTO] * 120)
    assert sum(result is not None for result in results) == LIMIT - RESERVE
    results = await charge_many(guard, [MANUAL] * 50)
    assert sum(result is not None for result in results) == RESERVE
    counts = (await guard.status()).counts
    assert (counts.requests_used, counts.manual_used) == (LIMIT, RESERVE)


async def test_http_level_concurrency_stops_exactly_at_the_automatic_budget(
    harness: Harness, router: respx.MockRouter
) -> None:
    route = router.get("/fixtures").respond(json=load(EMPTY))

    async def one(index: int) -> bool:
        try:
            await harness.api.get_json("fixtures", day_params(index), purpose=AUTO)
        except QuotaExceededError:
            return False
        return True

    outcomes = await asyncio.gather(*(one(index) for index in range(150)))
    assert sum(outcomes) == LIMIT - RESERVE
    assert route.call_count == LIMIT - RESERVE  # refused requests are never sent
    manual = await asyncio.gather(
        *(harness.api.get_json("fixtures", day_params(1000 + i), purpose=MANUAL) for i in range(30))
    )
    assert len(manual) == RESERVE
    assert (await harness.quota.status()).counts.requests_used == LIMIT


async def test_cache_hits_are_not_charged(harness: Harness, router: respx.MockRouter) -> None:
    router.get("/fixtures").respond(json=load(EMPTY))
    await harness.api.get_json("fixtures", day_params(1), purpose=AUTO)
    before = (await harness.quota.status()).counts
    for purpose in (AUTO, MANUAL, AUTO):
        assert (await harness.api.get_json("fixtures", day_params(1), purpose=purpose)).from_cache
    assert (await harness.quota.status()).counts == before


@pytest.mark.xfail(
    strict=True,
    reason="QA-T3-D1: no single-flight; concurrent identical requests all miss the cache",
)
async def test_concurrent_identical_requests_cost_one_request(
    harness: Harness, router: respx.MockRouter
) -> None:
    async def slow_provider(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01)  # a real network round trip yields to the event loop
        return httpx.Response(200, json=load(EMPTY))

    route = router.get("/fixtures").mock(side_effect=slow_provider)
    await asyncio.gather(
        *(harness.api.get_json("fixtures", day_params(1), purpose=AUTO) for _ in range(20))
    )
    assert route.call_count == 1


async def test_quota_exhaustion_mid_retry_stops_without_sending(
    harness: Harness, router: respx.MockRouter
) -> None:
    await harness.quota.charge(AUTO, LIMIT - RESERVE - 1)  # one automatic request left
    route = router.get("/fixtures").respond(500)
    with pytest.raises(QuotaExceededError):
        await harness.api.get_json("fixtures", day_params(1), purpose=AUTO)
    assert route.call_count == 1
    assert (await harness.quota.status()).counts.requests_used == LIMIT - RESERVE


# --------------------------------------------------------------------------- retries


async def test_each_retry_attempt_is_one_charged_request(
    harness: Harness, router: respx.MockRouter
) -> None:
    route = router.get("/fixtures").mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(502),
            httpx.Response(200, json=load(EMPTY)),
        ]
    )
    await harness.api.get_json("fixtures", day_params(1), purpose=MANUAL)
    counts = (await harness.quota.status()).counts
    assert route.call_count == counts.requests_used == 3
    assert counts.manual_used == 3
    assert harness.sleep.delays == [0.5, 1.0]  # equal jitter with jitter() == 0


async def test_exhausted_retries_cost_exactly_max_attempts(
    harness: Harness, router: respx.MockRouter
) -> None:
    route = router.get("/fixtures").respond(429)
    with pytest.raises(ProviderUnavailableError):
        await harness.api.get_json("fixtures", day_params(1), purpose=AUTO)
    attempts = harness.api.config.max_attempts
    assert route.call_count == attempts
    assert (await harness.quota.status()).counts.requests_used == attempts
    assert len(harness.sleep.delays) == attempts - 1  # no sleep after the last attempt


@pytest.mark.parametrize(
    ("header", "expected_delay"),
    [("7", 7.0), ("9999", 120.0), ("0", 0.5), ("-5", 0.5), ("nan", 0.5), ("inf", 120.0)],
)
async def test_429_retry_after_is_honoured_and_capped(
    harness: Harness, router: respx.MockRouter, header: str, expected_delay: float
) -> None:
    router.get("/fixtures").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": header}),
            httpx.Response(200, json=load(EMPTY)),
        ]
    )
    await harness.api.get_json("fixtures", day_params(1), purpose=AUTO)
    assert harness.sleep.delays == pytest.approx([expected_delay])


@settings(max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(value=st.one_of(st.none(), st.text(max_size=40), st.floats(allow_nan=True)))
def test_parse_retry_after_never_raises_and_is_never_negative(
    value: str | float | None,
) -> None:
    parsed = parse_retry_after(None if value is None else str(value))
    assert parsed is None or math.isnan(parsed) or parsed >= 0  # NaN is harmless downstream


@pytest.mark.parametrize("status", [401, 403, 404])
async def test_auth_and_not_found_errors_are_not_retried_nor_cached(
    harness: Harness, router: respx.MockRouter, status: int
) -> None:
    route = router.get("/fixtures").respond(status)
    for expected_calls in (1, 2):
        with pytest.raises(ProviderHttpError) as error:
            await harness.api.get_json("fixtures", day_params(1), purpose=AUTO)
        assert error.value.status_code == status
        assert route.call_count == expected_calls  # one attempt per call, none cached
    assert harness.sleep.delays == []
    assert (await harness.quota.status()).counts.requests_used == 2


async def test_provider_headers_reconcile_even_on_failed_responses(
    harness: Harness, router: respx.MockRouter
) -> None:
    headers = {"x-ratelimit-requests-limit": "100", "x-ratelimit-requests-remaining": "40"}
    router.get("/fixtures").mock(
        side_effect=[httpx.Response(500, headers=headers), httpx.Response(200, json=load(EMPTY))]
    )
    await harness.api.get_json("fixtures", day_params(1), purpose=AUTO)
    status = await harness.quota.status()
    # Provider said 60 used after the failed attempt; the retry is one more request.
    assert status.counts.requests_used == 61
    assert (status.remaining, status.automatic_available) == (39, 9)  # 30 stay reserved


# --------------------------------------------------------------------------- error envelopes


@pytest.mark.parametrize("name", ["error_token.json", "error_plan.json"])
async def test_error_envelopes_with_http_200_are_never_cached(
    harness: Harness, router: respx.MockRouter, name: str
) -> None:
    route = router.get("/fixtures").mock(
        side_effect=[
            httpx.Response(200, json=load(name)),
            httpx.Response(200, json=load(name)),
            httpx.Response(200, json=load(EMPTY)),
        ]
    )
    for _ in range(2):
        with pytest.raises(ApiFootballError):
            await harness.football.fixtures_by_date(date(2030, 1, 1), purpose=AUTO)
    assert not harness.api.cached("fixtures", {"date": "2030-01-01"})
    assert await harness.football.fixtures_by_date(date(2030, 1, 1), purpose=AUTO) == []
    assert route.call_count == 3


async def test_empty_errors_list_and_object_are_success(
    harness: Harness, router: respx.MockRouter
) -> None:
    body = load(EMPTY)
    assert isinstance(body, dict)
    for errors in ([], {}):
        router.get("/fixtures").respond(json={**body, "errors": errors})
        harness.api.invalidate("fixtures", {"date": "2030-01-02"})
        assert await harness.football.fixtures_by_date(date(2030, 1, 2), purpose=AUTO) == []


@pytest.mark.xfail(
    strict=True,
    reason="QA-T3-D2: an unparsable 200 body is cached for the full TTL and replayed",
)
async def test_unparsable_envelope_is_not_cached(
    harness: Harness, router: respx.MockRouter
) -> None:
    route = router.get("/fixtures").mock(
        side_effect=[
            httpx.Response(200, json={"errors": {"rateLimit": 429}, "response": []}),
            httpx.Response(200, json=load(EMPTY)),
        ]
    )
    day = date(2030, 1, 3)
    with pytest.raises(ExternalServiceError):
        await harness.football.fixtures_by_date(day, purpose=AUTO)
    harness.time.seconds += 1
    assert await harness.football.fixtures_by_date(day, purpose=AUTO) == []
    assert route.call_count == 2


def test_malformed_records_are_skipped_not_fatal() -> None:
    envelope = Envelope.model_validate(
        {"response": [{"fixture": 1}, "text", None, 5, {"fixture": {"id": "x"}}]}
    )
    assert validate_records(envelope, FixtureItem, endpoint="fixtures") == []


# --------------------------------------------------------------------------- secrets


def _leaks(secret: str, *things: object) -> list[str]:
    return [repr(thing)[:80] for thing in things if secret in repr(thing) or secret in str(thing)]


def _exception_chain(error: BaseException | None) -> list[BaseException]:
    chain: list[BaseException] = []
    while error is not None:
        chain.append(error)
        error = error.__cause__ or error.__context__
    return chain


async def provoke(harness: Harness, caught: list[BaseException]) -> None:
    """Run one failing/succeeding league request, collecting its exception."""
    try:
        await harness.football.fixtures_by_league(
            39, 2024, purpose=AUTO, date_from=None, date_to=None
        )
    except Exception as error:  # every failure type must be inspected for leaks
        caught.append(error)
    harness.api.invalidate("fixtures", {"league": 39, "season": 2024})


async def test_secret_never_reaches_logs_cache_keys_or_exceptions(
    fake_time: FakeTime, router: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Replace the module loggers with recording ones: structlog may have cached the
    # processors of an earlier test (cache_logger_on_first_use), which hides ``capture_logs``.
    recorder = structlog.testing.LogCapture()
    for module in (http_module, api_football_module):
        monkeypatch.setattr(
            module, "logger", structlog.wrap_logger(structlog.ReturnLogger(), [recorder])
        )
    secret = TEST_KEY
    scenarios: list[object] = [
        httpx.Response(200, json=load(EMPTY)),
        httpx.Response(500),
        httpx.Response(429, headers={"retry-after": "1"}),
        httpx.Response(401),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json=load("error_token.json")),
        httpx.ReadTimeout(f"timeout talking to host with key {secret}"),
        httpx.ConnectError("connection refused"),
    ]
    caught: list[BaseException] = []
    async with httpx.AsyncClient(
        base_url="https://v3.football.api-sports.io", headers={"x-apisports-key": secret}
    ) as http:
        harness = build_harness(fake_time, http)
        for scenario in scenarios:
            router.get("/fixtures").mock(side_effect=[scenario] * 8)
            await provoke(harness, caught)
        # What the client considers cached (keys) must be key-free as well.
        keys = list(harness.cache._entries)
    logs = list(recorder.entries)
    assert logs, "scenarios should have produced log events"
    assert caught, "scenarios should have raised"
    assert not _leaks(secret, *logs, *keys)
    chains = [link for error in caught for link in _exception_chain(error)]
    # The ReadTimeout message was written by the *test* with the key inside; the client must
    # not forward third-party exception text into its own exception or log events.
    own = [link for link in chains if type(link).__module__.startswith("app.")]
    assert not _leaks(secret, *own)
    assert not _leaks(secret, *logs)


async def test_api_error_text_is_the_only_provider_text_in_exceptions(
    harness: Harness, router: respx.MockRouter
) -> None:
    router.get("/fixtures").respond(json=load("error_token.json"))
    with pytest.raises(ApiFootballError) as error:
        await harness.football.fixtures_by_date(date(2030, 1, 4), purpose=AUTO)
    assert TEST_KEY not in str(error.value)
    assert json.dumps(error.value.errors).count(TEST_KEY) == 0
