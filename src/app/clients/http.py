"""Base async HTTP client for data providers: rate limit, quota, retry and TTL cache.

Every provider request goes through :meth:`ApiClient.get_json`, which in order:

1. answers from the TTL cache when the same endpoint+params was fetched recently (no quota
   spent; plan §9 "the same data is not requested twice within 10 minutes");
2. waits for a token of the per-provider :class:`~app.clients.rate_limit.TokenBucket`;
3. charges the daily :class:`~app.clients.quota.QuotaGuard` (refuses *before* sending);
4. sends the request with a per-request timeout;
5. retries 429, 5xx, timeouts and transport errors with jittered exponential backoff that
   honours ``Retry-After`` (every attempt is a real request and is charged);
6. reconciles the quota with the provider's remaining-requests headers when present.

Authentication is a header set once on the ``httpx.AsyncClient`` (see
:func:`create_http_client`); URLs and parameters, which are logged, never contain the key.
"""

import asyncio
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from typing import Final

import httpx
from pydantic import JsonValue
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception_type, stop_after_attempt

from app.clients.cache import QueryParams, TtlCache, cache_key
from app.clients.quota import QuotaGuard, RequestPurpose
from app.clients.rate_limit import Sleep, TokenBucket
from app.errors import ExternalServiceError
from app.logging import get_logger

logger = get_logger(__name__)

DEDUPE_TTL_SECONDS: Final = 10 * 60.0


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


@dataclass(frozen=True, slots=True)
class JsonResponse:
    """A decoded provider response."""

    status_code: int
    payload: JsonValue
    from_cache: bool = False


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


def create_http_client(config: ProviderConfig, api_key: str) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` for ``config`` that authenticates with a header.

    The caller owns the client and must ``await client.aclose()``.
    """
    return httpx.AsyncClient(
        base_url=config.base_url,
        headers={config.auth_header: api_key, "accept": "application/json"},
        timeout=config.timeout_seconds,
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


class ApiClient:
    """GET-only JSON client for one provider (see the module docstring for the pipeline)."""

    def __init__(  # noqa: PLR0913 - collaborators are injected for testability
        self,
        config: ProviderConfig,
        *,
        http: httpx.AsyncClient,
        quota: QuotaGuard,
        limiter: TokenBucket | None = None,
        cache: TtlCache[JsonValue] | None = None,
        sleep: Sleep = asyncio.sleep,
        jitter: Callable[[], float] = random.random,  # noqa: S311 - backoff jitter, not crypto
    ) -> None:
        """Wire the client; ``sleep``/``jitter`` are injectable so tests never wait."""
        self._config = config
        self._http = http
        self._quota = quota
        self._limiter = limiter or TokenBucket(
            config.requests_per_minute, capacity=config.burst, sleep=sleep
        )
        self._cache: TtlCache[JsonValue] = cache if cache is not None else TtlCache()
        self._sleep = sleep
        self._jitter = jitter

    @property
    def config(self) -> ProviderConfig:
        """Settings of the provider this client talks to."""
        return self._config

    @property
    def quota(self) -> QuotaGuard:
        """The daily quota guard (status for cost estimates and ``/holat``)."""
        return self._quota

    def cached(self, path: str, params: QueryParams) -> bool:
        """Whether ``path``+``params`` would be answered from cache (costs nothing)."""
        return self._cache.get(cache_key(path, params)) is not None

    def invalidate(self, path: str, params: QueryParams) -> None:
        """Forget a cached body (e.g. one that turned out to carry provider errors)."""
        self._cache.discard(cache_key(path, params))

    async def get_json(
        self,
        path: str,
        params: QueryParams,
        *,
        purpose: RequestPurpose,
        cache_ttl: float | None = None,
    ) -> JsonResponse:
        """GET ``path`` and return its JSON body.

        Args:
            path: endpoint path relative to the provider base URL.
            params: query parameters (also the cache key).
            purpose: charged against the automatic or the manual part of the quota.
            cache_ttl: seconds to cache the body; ``None`` = the provider default,
                ``0`` = do not cache.

        Raises:
            QuotaExceededError: today's quota for ``purpose`` is spent (nothing sent).
            ProviderHttpError: the provider rejected the request.
            ProviderUnavailableError: retries were exhausted.
        """
        key = cache_key(path, params)
        cached = self._cache.get(key)
        if cached is not None:
            logger.debug("provider_cache_hit", provider=self._config.name, request=key)
            return JsonResponse(HTTPStatus.OK, cached, from_cache=True)
        response = await self._get_with_retries(path, params, purpose)
        ttl = self._config.default_cache_ttl_seconds if cache_ttl is None else cache_ttl
        self._cache.set(key, response.payload, ttl=ttl)
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
        await self._limiter.acquire()
        await self._quota.charge(purpose)
        try:
            response = await self._http.get(path, params=dict(params))
        except httpx.TimeoutException as exc:
            raise _RetryableError(f"timeout: {type(exc).__name__}") from exc
        except httpx.TransportError as exc:
            raise _RetryableError(f"transport error: {type(exc).__name__}") from exc
        logger.info(
            "provider_request",
            provider=self._config.name,
            path=path,
            params=dict(params),
            status=response.status_code,
            purpose=purpose.value,
        )
        await self._reconcile_quota(response.headers)
        return self._decode(response)

    def _decode(self, response: httpx.Response) -> JsonResponse:
        status = response.status_code
        if status == HTTPStatus.TOO_MANY_REQUESTS or status >= HTTPStatus.INTERNAL_SERVER_ERROR:
            retry_after = parse_retry_after(response.headers.get("retry-after"))
            raise _RetryableError(f"HTTP {status}", retry_after)
        if status >= HTTPStatus.BAD_REQUEST:
            raise ProviderHttpError(self._config.name, status, response.reason_phrase)
        try:
            payload: JsonValue = response.json()
        except ValueError as exc:
            raise ProviderHttpError(self._config.name, status, "body is not JSON") from exc
        return JsonResponse(status, payload)

    async def _reconcile_quota(self, headers: httpx.Headers) -> None:
        limit = _int_header(headers, self._config.limit_header)
        remaining = _int_header(headers, self._config.remaining_header)
        if limit is None or remaining is None:
            return
        status = await self._quota.reconcile(provider_limit=limit, provider_remaining=remaining)
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
