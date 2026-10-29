"""Base ApiClient: cache, quota charging, retry/backoff, Retry-After, header reconcile."""

from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
import respx
import structlog

from app.clients.http import (
    ProviderHttpError,
    ProviderUnavailableError,
    create_http_client,
    parse_retry_after,
)
from app.clients.quota import QuotaExceededError, RequestPurpose, UsageCounts
from tests.clients.conftest import (
    BASE_URL,
    EMPTY_BODY,
    TEST_CONFIG,
    TEST_KEY,
    FakeTime,
    Harness,
    build_harness,
)

AUTO, MANUAL = RequestPurpose.AUTOMATIC, RequestPurpose.MANUAL
PARAMS = {"date": "2024-10-26"}


async def test_get_json_returns_body_and_charges_one_request(
    harness: Harness, router: respx.MockRouter
) -> None:
    route = router.get("/fixtures").respond(json=EMPTY_BODY)
    response = await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)
    assert response.status_code == 200
    assert not response.from_cache
    assert isinstance(response.payload, dict)
    assert route.calls.last.request.url.params["date"] == "2024-10-26"
    assert route.calls.last.request.headers[TEST_CONFIG.auth_header] == TEST_KEY
    assert (await harness.quota.status()).counts == UsageCounts(1, 0)


async def test_same_request_within_ten_minutes_is_served_from_cache(
    harness: Harness, router: respx.MockRouter
) -> None:
    route = router.get("/fixtures").respond(json=EMPTY_BODY)
    await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)
    harness.time.seconds += 599
    cached = await harness.api.get_json("fixtures", PARAMS, purpose=MANUAL)
    assert cached.from_cache
    assert harness.api.cached("fixtures", PARAMS)
    harness.time.seconds += 2
    assert not harness.api.cached("fixtures", PARAMS)
    await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)
    assert route.call_count == 2
    assert (await harness.quota.status()).counts.requests_used == 2


async def test_cache_ttl_zero_disables_caching(harness: Harness, router: respx.MockRouter) -> None:
    route = router.get("/fixtures").respond(json=EMPTY_BODY)
    for _ in range(2):
        await harness.api.get_json("fixtures", PARAMS, purpose=AUTO, cache_ttl=0)
    assert route.call_count == 2


async def test_retries_5xx_then_succeeds_with_exponential_backoff(
    harness: Harness, router: respx.MockRouter
) -> None:
    route = router.get("/fixtures").mock(
        side_effect=[
            httpx.Response(502),
            httpx.Response(503),
            httpx.Response(200, json=EMPTY_BODY),
        ]
    )
    response = await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)
    assert response.status_code == 200
    assert route.call_count == 3
    # jitter=0 -> half of the capped exponential delay: 0.5 s, 1 s.
    assert harness.sleep.delays == [0.5, 1.0]
    assert (await harness.quota.status()).counts.requests_used == 3


async def test_429_honours_retry_after(harness: Harness, router: respx.MockRouter) -> None:
    router.get("/fixtures").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json=EMPTY_BODY),
        ]
    )
    await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)
    assert harness.sleep.delays == [7.0]


async def test_retry_after_beyond_the_cap_fails_fast(
    harness: Harness, router: respx.MockRouter
) -> None:
    route = router.get("/fixtures").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "86400"}),
            httpx.Response(200, json=EMPTY_BODY),
        ]
    )
    with pytest.raises(ProviderUnavailableError, match="86400"):
        await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)
    assert route.call_count == 1
    assert harness.sleep.delays == []
    assert not harness.api.cached("fixtures", PARAMS)


async def test_retry_after_at_the_cap_is_still_honoured(
    harness: Harness, router: respx.MockRouter
) -> None:
    cap = TEST_CONFIG.max_retry_after_seconds
    router.get("/fixtures").mock(
        side_effect=[
            httpx.Response(503, headers={"Retry-After": str(int(cap))}),
            httpx.Response(200, json=EMPTY_BODY),
        ]
    )
    await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)
    assert harness.sleep.delays == [cap]


async def test_timeouts_are_retried_and_exhaustion_raises(
    harness: Harness, router: respx.MockRouter
) -> None:
    route = router.get("/fixtures").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderUnavailableError, match="4 attempts"):
        await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)
    assert route.call_count == TEST_CONFIG.max_attempts
    assert harness.sleep.delays == [0.5, 1.0, 2.0]
    assert not harness.api.cached("fixtures", PARAMS)


async def test_transport_errors_are_retried(harness: Harness, router: respx.MockRouter) -> None:
    router.get("/fixtures").mock(
        side_effect=[
            httpx.ConnectError("refused"),
            httpx.Response(200, json=EMPTY_BODY),
        ]
    )
    assert (await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)).status_code == 200


async def test_client_errors_are_not_retried(harness: Harness, router: respx.MockRouter) -> None:
    route = router.get("/fixtures").respond(403)
    with pytest.raises(ProviderHttpError) as error:
        await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)
    assert error.value.status_code == 403
    assert route.call_count == 1


async def test_non_json_body_is_a_provider_error(
    harness: Harness, router: respx.MockRouter
) -> None:
    router.get("/fixtures").respond(200, text="<html>blocked by proxy</html>")
    with pytest.raises(ProviderHttpError, match="not JSON"):
        await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)


async def test_quota_refusal_sends_nothing(
    fake_time: FakeTime, http_client: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    harness = build_harness(fake_time, http_client, daily_limit=1)  # reserve rounds to 0
    route = router.get("/fixtures").respond(json=EMPTY_BODY)
    await harness.api.get_json("fixtures", PARAMS, purpose=AUTO, cache_ttl=0)
    with pytest.raises(QuotaExceededError):
        await harness.api.get_json("fixtures", PARAMS, purpose=MANUAL, cache_ttl=0)
    assert route.call_count == 1


async def test_retries_stop_when_quota_runs_out(
    fake_time: FakeTime, http_client: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    harness = build_harness(fake_time, http_client, daily_limit=2)  # automatic: 1
    route = router.get("/fixtures").respond(500)
    with pytest.raises(QuotaExceededError):
        await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)
    assert route.call_count == 1


async def test_provider_quota_headers_are_reconciled(
    harness: Harness, router: respx.MockRouter
) -> None:
    router.get("/fixtures").respond(
        json=EMPTY_BODY,
        headers={"x-ratelimit-requests-limit": "100", "x-ratelimit-requests-remaining": "60"},
    )
    await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)
    assert (await harness.quota.status()).counts.requests_used == 40


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"x-ratelimit-requests-limit": "100"},
        {"x-ratelimit-requests-limit": "x", "x-ratelimit-requests-remaining": "1"},
    ],
)
async def test_missing_or_bad_quota_headers_are_ignored(
    harness: Harness, router: respx.MockRouter, headers: dict[str, str]
) -> None:
    router.get("/fixtures").respond(json=EMPTY_BODY, headers=headers)
    await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)
    assert (await harness.quota.status()).counts.requests_used == 1


async def test_rate_limiter_paces_requests(
    fake_time: FakeTime, http_client: httpx.AsyncClient, router: respx.MockRouter
) -> None:
    config = replace(TEST_CONFIG, requests_per_minute=10, burst=1)
    harness = build_harness(fake_time, http_client, config=config)
    router.get("/fixtures").respond(json=EMPTY_BODY)
    for day in ("2024-10-26", "2024-10-27", "2024-10-28"):
        await harness.api.get_json("fixtures", {"date": day}, purpose=AUTO)
    assert harness.sleep.delays == pytest.approx([6.0, 6.0])


async def test_logs_never_contain_the_key(harness: Harness, router: respx.MockRouter) -> None:
    router.get("/fixtures").respond(json=EMPTY_BODY)
    with structlog.testing.capture_logs() as logs:
        await harness.api.get_json("fixtures", PARAMS, purpose=AUTO)
    assert logs
    assert TEST_KEY not in repr(logs)


async def test_create_http_client_sets_auth_header_and_base_url() -> None:
    async with create_http_client(TEST_CONFIG, TEST_KEY) as client:
        assert client.headers[TEST_CONFIG.auth_header] == TEST_KEY
        assert str(client.base_url).startswith(BASE_URL)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("12", 12.0),
        ("-3", 0.0),
        ("Sat, 26 Oct 2024 08:00:30 GMT", 30.0),
        ("garbage", None),
    ],
)
def test_parse_retry_after(value: str | None, expected: float | None) -> None:
    now = datetime(2024, 10, 26, 8, 0, tzinfo=UTC)
    assert parse_retry_after(value, now=now) == expected
