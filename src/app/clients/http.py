"""Base async HTTP client for data providers: rate limit, quota, retry and TTL cache.

Every provider request goes through :meth:`ApiClient.get_json`, which in order:

1. answers from the TTL cache when the same endpoint+params was fetched recently (no quota
   spent; plan §9 "the same data is not requested twice within 10 minutes");
2. joins an identical request that is already in flight (single-flight: concurrent callers
   share one request and one charge);
3. waits for a token of the per-provider :class:`~app.clients.rate_limit.TokenBucket`
   (manual requests go before queued automatic ones);
4. charges the daily :class:`~app.clients.quota.QuotaGuard` (refuses *before* sending);
   a provider without a daily limit is built without one and skips this step;
5. sends the request with a per-request timeout;
6. retries 429, 5xx, timeouts, transport errors and provider-flagged rate-limit bodies with
   jittered exponential backoff that honours ``Retry-After`` (every attempt is a real
   request and is charged); a ``Retry-After`` beyond the cap fails fast instead;
7. reconciles the quota with the provider's remaining-requests headers when present.

When a :class:`~app.domain.CallRecorder` is given, every step above that ends a call reports
it: a cache answer (``cached``, no cost), a refusal by the quota (``quota_blocked``), each HTTP
attempt (``ok`` or ``error``, one request charged). Reporting is best effort by contract, so it
never changes what the caller gets.

Authentication is a header set once on the ``httpx.AsyncClient`` (see
:func:`create_http_client`); URLs and parameters, which are logged, never contain the key.
"""

import asyncio
import json
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from functools import partial
from http import HTTPStatus
from typing import Final

import httpx
from pydantic import JsonValue
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception_type, stop_after_attempt

from app.clients.cache import QueryParams, TtlCache, cache_key
from app.clients.quota import QuotaExceededError, QuotaGuard, RequestPurpose, UsageKey
from app.clients.rate_limit import Sleep, TokenBucket
from app.domain import CallOutcome, CallRecord, CallRecorder, Provider
from app.errors import ConfigurationError, ExternalServiceError
from app.logging import get_logger
from app.tls import get_ssl_context

logger = get_logger(__name__)

DEDUPE_TTL_SECONDS: Final = 10 * 60.0
_MS_PER_SECOND: Final = 1000

type UtcClock = Callable[[], datetime]
type BodyRetryCheck = Callable[[JsonValue], str | None]


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderConfig:
    """Connection, pacing and retry settings of one provider."""

    name: str
    base_url: str
    auth_header: str
    requests_per_minute: float
    burst: int = 1
    timeout_seconds: float = 20.0
    max_attempts: int = 4
    backoff_initial_seconds: float = 1.0
    backoff_max_seconds: float = 30.0
    max_retry_after_seconds: float = 120.0
    default_cache_ttl_seconds: float = DEDUPE_TTL_SECONDS
    # Daily-quota headers (API-Sports: x-ratelimit-requests-limit / -remaining).
    limit_header: str | None = None
    remaining_header: str | None = None
    # Some providers report a rate limit inside an HTTP 200 body; the check returns the
    # reason to retry (like a 429) or ``None`` when the body is not such a case.
    retryable_body: BodyRetryCheck | None = None
    # The name this provider's calls are logged under in the call log.
    log_provider: Provider
    # Sent with every request besides the key (e.g. Football-Data's ``X-Unfold-*`` switches).
    extra_headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class JsonResponse:
    """A decoded provider response."""

    status_code: int
    payload: JsonValue
    from_cache: bool = False
    # When the provider answered (for a cached body: when it originally answered).
    fetched_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class _CachedBody:
    payload: JsonValue
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class PayloadShape:
    """A reduction applied to a body *before* it is cached and returned.

    Provider bodies carry far more than the pipeline uses (every league of a day, every
    bookmaker of a fixture). Cutting them down at the door keeps the cache and the parse cost
    small. ``variant`` names the reduction and is part of the cache key, so a narrower body is
    never served to a caller that asked for a wider one.
    """

    variant: str
    apply: Callable[[JsonValue], JsonValue]


# A parsed JSON value costs several times its wire size in Python objects.
PARSED_SIZE_FACTOR: Final = 4


class ProviderHttpError(ExternalServiceError):
    """The provider rejected the request (4xx other than 429) or sent a non-JSON body."""

    def __init__(self, provider: str, status_code: int, reason: str) -> None:
        """Keep the status for callers that branch on it (e.g. 401 = bad key)."""
        self.status_code = status_code
        super().__init__(f"{provider}: HTTP {status_code}: {reason}")


class ProviderUnavailableError(ExternalServiceError):
    """The provider kept failing (429/5xx/timeouts) after every retry."""


class _RetryableError(Exception):
    """One attempt failed in a way that may succeed later."""

    def __init__(self, reason: str, retry_after: float | None = None) -> None:
        self.reason = reason
        self.retry_after = retry_after
        super().__init__(reason)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def create_http_client(config: ProviderConfig, api_key: str) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` for ``config`` that authenticates with a header.

    The caller owns the client and must ``await client.aclose()``.
    """
    return httpx.AsyncClient(
        base_url=config.base_url,
        headers={
            **dict(config.extra_headers),
            config.auth_header: api_key,
            "accept": "application/json",
        },
        timeout=config.timeout_seconds,
        verify=get_ssl_context() or True,
    )


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Seconds to wait from a ``Retry-After`` header (delta-seconds or HTTP date)."""
    if value is None or not value.strip():
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    return max((moment - reference).total_seconds(), 0.0)


def _key(path: str, params: QueryParams, shape: PayloadShape | None) -> str:
    return cache_key(path, params) + ("" if shape is None else f"#{shape.variant}")


class ApiClient:
    """GET-only JSON client for one provider (see the module docstring for the pipeline)."""

    def __init__(  # noqa: PLR0913 - collaborators are injected for testability
        self,
        config: ProviderConfig,
        *,
        http: httpx.AsyncClient,
        quota: QuotaGuard | None,
        limiter: TokenBucket | None = None,
        cache: TtlCache[_CachedBody] | None = None,
        sleep: Sleep = asyncio.sleep,
        jitter: Callable[[], float] = random.random,  # noqa: S311 - backoff jitter, not crypto
        clock: UtcClock = _utc_now,
        recorder: CallRecorder | None = None,
    ) -> None:
        """Wire the client; ``sleep``/``jitter`` are injectable so tests never wait."""
        self._config = config
        self._http = http
        self._quota = quota
        self._limiter = limiter or TokenBucket(
            config.requests_per_minute, capacity=config.burst, sleep=sleep
        )
        self._cache: TtlCache[_CachedBody] = cache if cache is not None else TtlCache()
        self._sleep = sleep
        self._jitter = jitter
        self._clock = clock
        self._recorder = recorder
        self._inflight: dict[str, asyncio.Future[JsonResponse]] = {}

    @property
    def config(self) -> ProviderConfig:
        """Settings of the provider this client talks to."""
        return self._config

    @property
    def quota(self) -> QuotaGuard:
        """The daily quota guard (status for cost estimates and ``/holat``).

        Raises:
            ConfigurationError: the client was built without a quota (no daily limit).
        """
        if self._quota is None:
            msg = f"{self._config.name} has no daily quota"
            raise ConfigurationError(msg)
        return self._quota

    def cached(self, path: str, params: QueryParams, shape: PayloadShape | None = None) -> bool:
        """Whether ``path``+``params`` would be answered from cache (costs nothing)."""
        return self._cache.get(_key(path, params, shape)) is not None

    def invalidate(self, path: str, params: QueryParams, shape: PayloadShape | None = None) -> None:
        """Forget a cached body (e.g. one that turned out to carry provider errors)."""
        self._cache.discard(_key(path, params, shape))

    async def get_json(
        self,
        path: str,
        params: QueryParams,
        *,
        purpose: RequestPurpose,
        cache_ttl: float | None = None,
        shape: PayloadShape | None = None,
    ) -> JsonResponse:
        """GET ``path`` and return its JSON body.

        Args:
            path: endpoint path relative to the provider base URL.
            params: query parameters (also the cache key).
            purpose: charged against the automatic or the manual part of the quota.
            cache_ttl: seconds to cache the body; ``None`` = the provider default,
                ``0`` = always ask the provider (the cache is neither read nor written;
                use it when a price must be fresh, e.g. closing odds).
            shape: reduce the body before it is cached and returned (see :class:`PayloadShape`).

        Raises:
            QuotaExceededError: today's quota for ``purpose`` is spent (nothing sent).
            ProviderHttpError: the provider rejected the request.
            ProviderUnavailableError: retries were exhausted.
        """
        key = _key(path, params, shape)
        if cache_ttl is None or cache_ttl > 0:
            cached = self._cache.get(key)
            if cached is not None:
                logger.debug("provider_cache_hit", provider=self._config.name, request=key)
                await self._report(path, params, CallOutcome.CACHED, status=HTTPStatus.OK)
                return JsonResponse(
                    HTTPStatus.OK, cached.payload, from_cache=True, fetched_at=cached.fetched_at
                )
        flight = self._inflight.get(key)
        if flight is None:
            flight = asyncio.ensure_future(
                self._fetch(key, path, params, purpose, cache_ttl, shape)
            )
            self._inflight[key] = flight
            flight.add_done_callback(partial(self._landed, key))
        else:
            logger.debug("provider_single_flight", provider=self._config.name, request=key)
        # Shielded: a cancelled caller must not cancel the request other callers wait for.
        return await asyncio.shield(flight)

    def _landed(self, key: str, flight: "asyncio.Future[JsonResponse]") -> None:
        if self._inflight.get(key) is flight:
            del self._inflight[key]
        if not flight.cancelled():
            flight.exception()  # mark retrieved: callers re-raise it themselves

    async def _fetch(  # noqa: PLR0913, PLR0917 - one value per request facet
        self,
        key: str,
        path: str,
        params: QueryParams,
        purpose: RequestPurpose,
        cache_ttl: float | None,
        shape: PayloadShape | None,
    ) -> JsonResponse:
        response = await self._get_with_retries(path, params, purpose)
        if shape is not None:
            response = replace(response, payload=shape.apply(response.payload))
        ttl = self._config.default_cache_ttl_seconds if cache_ttl is None else cache_ttl
        fetched_at = response.fetched_at or self._clock()
        if ttl > 0:  # a body that is never cached is not measured either
            size = len(json.dumps(response.payload, separators=(",", ":"))) * PARSED_SIZE_FACTOR
            self._cache.set(key, _CachedBody(response.payload, fetched_at), ttl=ttl, size=size)
        return response

    async def _get_with_retries(
        self, path: str, params: QueryParams, purpose: RequestPurpose
    ) -> JsonResponse:
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._config.max_attempts),
            wait=self._wait_seconds,
            retry=retry_if_exception_type(_RetryableError),
            sleep=self._sleep,
            reraise=True,
        )
        try:
            async for attempt in retrying:
                with attempt:
                    return await self._send_once(path, params, purpose)
        except _RetryableError as exc:
            logger.warning(
                "provider_retries_exhausted",
                provider=self._config.name,
                path=path,
                params=dict(params),
                reason=exc.reason,
            )
            msg = (
                f"{self._config.name}: {path} failed after "
                f"{self._config.max_attempts} attempts ({exc.reason})"
            )
            raise ProviderUnavailableError(msg) from exc
        raise AssertionError  # pragma: no cover - AsyncRetrying either returns or raises

    async def _send_once(
        self, path: str, params: QueryParams, purpose: RequestPurpose
    ) -> JsonResponse:
        await self._limiter.acquire(urgent=purpose is RequestPurpose.MANUAL)
        usage_key = await self._charge(path, params, purpose)
        started = time.perf_counter()
        try:
            response = await self._http.get(path, params=dict(params))
        except httpx.TimeoutException as exc:
            await self._report_failure(path, params, started, f"timeout: {type(exc).__name__}")
            raise _RetryableError(f"timeout: {type(exc).__name__}") from exc
        except httpx.TransportError as exc:
            reason = f"transport error: {type(exc).__name__}"
            await self._report_failure(path, params, started, reason)
            raise _RetryableError(reason) from exc
        logger.info(
            "provider_request",
            provider=self._config.name,
            path=path,
            params=dict(params),
            status=response.status_code,
            purpose=purpose.value,
        )
        if usage_key is not None:
            await self._reconcile_quota(usage_key, response.headers)
        try:
            decoded = self._decode(response)
        except (_RetryableError, ProviderHttpError, ProviderUnavailableError) as exc:
            reason = exc.reason if isinstance(exc, _RetryableError) else str(exc)
            await self._report_failure(path, params, started, reason, response)
            raise
        await self._report(
            path,
            params,
            CallOutcome.OK,
            status=response.status_code,
            duration_ms=_elapsed_ms(started),
            body=response,
        )
        return decoded

    async def _charge(
        self, path: str, params: QueryParams, purpose: RequestPurpose
    ) -> UsageKey | None:
        """Charge one request; the key is the quota day it belongs to (``None``: no quota)."""
        if self._quota is None:
            return None
        try:
            return (await self._quota.charge(purpose)).key
        except QuotaExceededError as exc:
            await self._report(path, params, CallOutcome.QUOTA_BLOCKED, error=str(exc))
            raise

    async def _report_failure(
        self,
        path: str,
        params: QueryParams,
        started: float,
        reason: str,
        response: httpx.Response | None = None,
    ) -> None:
        """Report one charged attempt that failed (``response`` is ``None`` before an answer)."""
        await self._report(
            path,
            params,
            CallOutcome.ERROR,
            status=None if response is None else response.status_code,
            duration_ms=_elapsed_ms(started),
            body=response,
            error=reason,
        )

    async def _report(  # noqa: PLR0913 - one value per logged facet
        self,
        path: str,
        params: QueryParams,
        outcome: CallOutcome,
        *,
        status: int | None = None,
        duration_ms: int = 0,
        body: httpx.Response | None = None,
        error: str | None = None,
    ) -> None:
        """Hand one call to the recorder (a no-op without one). Cost: 1 per sent request."""
        if self._recorder is None:
            return
        sent = outcome in (CallOutcome.OK, CallOutcome.ERROR)
        await self._recorder.record(
            CallRecord(
                at=self._clock(),
                provider=self._config.log_provider,
                operation=path,
                outcome=outcome,
                duration_ms=duration_ms,
                request=json.dumps(dict(params), sort_keys=True, ensure_ascii=False),
                response=None if body is None else body.text,
                http_status=status,
                quota_cost=1 if sent else 0,
                size_bytes=None if body is None else len(body.content),
                error=error,
            )
        )

    def _decode(self, response: httpx.Response) -> JsonResponse:
        status = response.status_code
        if status == HTTPStatus.TOO_MANY_REQUESTS or status >= HTTPStatus.INTERNAL_SERVER_ERROR:
            retry_after = parse_retry_after(response.headers.get("retry-after"))
            cap = self._config.max_retry_after_seconds
            if retry_after is not None and retry_after > cap:
                # Waiting that long inside a request is useless to the caller: fail fast.
                msg = (
                    f"{self._config.name}: HTTP {status} asks to retry after "
                    f"{retry_after:.0f} s (more than the {cap:.0f} s limit)"
                )
                raise ProviderUnavailableError(msg)
            raise _RetryableError(f"HTTP {status}", retry_after)
        if status >= HTTPStatus.BAD_REQUEST:
            raise ProviderHttpError(self._config.name, status, response.reason_phrase)
        try:
            payload: JsonValue = response.json()
        except ValueError as exc:
            raise ProviderHttpError(self._config.name, status, "body is not JSON") from exc
        check = self._config.retryable_body
        reason = None if check is None else check(payload)
        if reason is not None:
            raise _RetryableError(reason)
        return JsonResponse(status, payload, fetched_at=self._clock())

    async def _reconcile_quota(self, key: UsageKey, headers: httpx.Headers) -> None:
        limit = _int_header(headers, self._config.limit_header)
        remaining = _int_header(headers, self._config.remaining_header)
        if self._quota is None or limit is None or remaining is None:
            return
        status = await self._quota.reconcile(
            key, provider_limit=limit, provider_remaining=remaining
        )
        logger.debug(
            "provider_quota",
            provider=self._config.name,
            provider_limit=limit,
            provider_remaining=remaining,
            counted_used=status.counts.requests_used,
        )

    def _wait_seconds(self, state: RetryCallState) -> float:
        """Equal-jitter exponential backoff, never shorter than the server's Retry-After."""
        config = self._config
        exponential = config.backoff_initial_seconds * 2.0 ** (state.attempt_number - 1)
        capped = min(exponential, config.backoff_max_seconds)
        delay = capped / 2 + self._jitter() * capped / 2
        error = state.outcome.exception() if state.outcome is not None else None
        if isinstance(error, _RetryableError) and error.retry_after is not None:
            delay = max(delay, min(error.retry_after, config.max_retry_after_seconds))
        return delay


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * _MS_PER_SECOND)


def _int_header(headers: Mapping[str, str], name: str | None) -> int | None:
    if name is None:
        return None
    value = headers.get(name)
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None
