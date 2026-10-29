"""Typed endpoint client for Football-Data.org v4 (``api.football-data.org``).

Thin by design, like :mod:`app.clients.api_football`: one method per endpoint, returning
validated :mod:`app.clients.football_data_org_models` records. Rate limit (30 requests a minute),
retry with backoff on 429/5xx, the TTL cache and single-flight all come from
:class:`~app.clients.http.ApiClient`; this module only knows the endpoints.

Endpoint rules that live here:

- ``/matches`` accepts a date range of at most 10 days, so longer ranges are split into
  windows (one request each);
- a competition is addressed by id or code (``PL``, ``CL``, ``PD``, ``BL1``, ``SA``, ``FL1``)
  and validated before it becomes part of a URL path;
- line-ups come with ``GET /matches/{id}`` (paid plans; on the free tier the lists are empty,
  which :attr:`MatchLineups.announced` reports).

The provider has no daily limit, only the per-minute one, so the client has no daily
:class:`~app.clients.quota.QuotaGuard`: the per-minute token bucket is the only limit.
"""

import re
from collections.abc import Iterator, Mapping
from datetime import date, timedelta
from typing import Final, Protocol

import httpx
from pydantic import JsonValue, ValidationError

from app.clients.cache import QueryParams, TtlCache
from app.clients.football_data_org_models import (
    Match,
    MatchLineups,
    MatchStatus,
    match_lineups,
)
from app.clients.http import ApiClient, ProviderConfig, create_http_client
from app.clients.quota import RequestPurpose
from app.errors import ExternalServiceError
from app.logging import get_logger

logger = get_logger(__name__)

PROVIDER: Final = "football-data-org"
REQUESTS_PER_MINUTE: Final = 30
MAX_DAYS_PER_MATCHES_REQUEST: Final = 10
"""``/matches`` rejects a ``dateFrom``..``dateTo`` range longer than this (both days count)."""
TOP_COMPETITIONS: Final = ("PL", "CL", "PD", "BL1", "SA", "FL1")
"""Free-tier leagues: England, Champions League, Spain, Germany, Italy, France."""
MATCH_DETAIL_TTL_SECONDS: Final = 5 * 60.0
"""Line-ups appear shortly before kick-off, so a match body is not kept as long as a schedule."""

# 2-5 letters/digits (``PL``, ``BL1``) or a numeric id (``2021``); nothing that could alter a path.
_COMPETITION: Final = re.compile(r"[A-Za-z0-9]{2,5}")

FOOTBALL_DATA_ORG_CONFIG: Final = ProviderConfig(
    name=PROVIDER,
    base_url="https://api.football-data.org/v4/",
    auth_header="X-Auth-Token",
    requests_per_minute=REQUESTS_PER_MINUTE,
)


class FootballDataClient(Protocol):
    """What the system needs from Football-Data.org; tests and fakes implement this."""

    async def matches_between(
        self, date_from: date, date_to: date, *, purpose: RequestPurpose
    ) -> list[Match]:
        """Matches with kick-off in ``date_from``..``date_to`` (UTC days, both included)."""
        ...

    async def competition_matches(
        self,
        competition: str | int,
        *,
        purpose: RequestPurpose,
        status: MatchStatus | None = MatchStatus.SCHEDULED,
    ) -> list[Match]:
        """Matches of one competition, by default the ones still to be played."""
        ...

    async def match(self, match_id: int, *, purpose: RequestPurpose) -> Match:
        """One match with teams, score, coaches and (paid plans) line-ups."""
        ...

    async def lineups(self, match_id: int, *, purpose: RequestPurpose) -> MatchLineups:
        """Starting elevens, benches and coaches of both sides of one match."""
        ...


class HttpFootballDataClient:
    """:class:`FootballDataClient` over an :class:`~app.clients.http.ApiClient`."""

    def __init__(self, api: ApiClient) -> None:
        """Wrap the provider-level client (rate limit, retry, cache)."""
        self._api = api

    async def matches_between(
        self, date_from: date, date_to: date, *, purpose: RequestPurpose
    ) -> list[Match]:
        """Matches in a date range; a range over 10 days costs one request per 10 days.

        Raises:
            ValueError: ``date_to`` is before ``date_from``.
        """
        matches: list[Match] = []
        for window_from, window_to in date_windows(date_from, date_to):
            params = {"dateFrom": window_from.isoformat(), "dateTo": window_to.isoformat()}
            matches.extend(await self._matches("matches", params, purpose))
        return matches

    async def competition_matches(
        self,
        competition: str | int,
        *,
        purpose: RequestPurpose,
        status: MatchStatus | None = MatchStatus.SCHEDULED,
    ) -> list[Match]:
        """Matches of one competition (code like ``PL`` or numeric id): 1 request.

        Raises:
            ValueError: ``competition`` is not a code or id.
        """
        code = competition_path(competition)
        params: dict[str, str | int] = {} if status is None else {"status": status.value}
        return await self._matches(f"competitions/{code}/matches", params, purpose)

    async def match(self, match_id: int, *, purpose: RequestPurpose) -> Match:
        """One match by id: 1 request.

        Raises:
            ProviderHttpError: unknown id (404) or a match the plan does not include (403).
            ExternalServiceError: the body is not a match.
        """
        path = f"matches/{match_id}"
        response = await self._api.get_json(
            path, {}, purpose=purpose, cache_ttl=MATCH_DETAIL_TTL_SECONDS
        )
        try:
            return Match.model_validate(response.payload)
        except ValidationError as exc:
            self._api.invalidate(path, {})  # never replay a body we cannot read
            msg = f"{PROVIDER} {path}: unexpected response format ({exc.error_count()} errors)"
            raise ExternalServiceError(msg) from exc

    async def lineups(self, match_id: int, *, purpose: RequestPurpose) -> MatchLineups:
        """Both sides' line-ups of a match: 1 request (empty lists until they are announced)."""
        return match_lineups(await self.match(match_id, purpose=purpose))

    async def _matches(
        self, path: str, params: QueryParams, purpose: RequestPurpose
    ) -> list[Match]:
        response = await self._api.get_json(path, params, purpose=purpose)
        records = _match_records(response.payload)
        if records is None:
            self._api.invalidate(path, params)
            msg = f"{PROVIDER} {path}: unexpected response format (no 'matches' list)"
            raise ExternalServiceError(msg)
        return validate_matches(records, path=path)


def date_windows(date_from: date, date_to: date) -> Iterator[tuple[date, date]]:
    """Split ``date_from``..``date_to`` (both included) into ranges the provider accepts."""
    if date_to < date_from:
        msg = f"date_to {date_to} is before date_from {date_from}"
        raise ValueError(msg)
    step = timedelta(days=MAX_DAYS_PER_MATCHES_REQUEST)
    start = date_from
    while start <= date_to:
        yield start, min(start + step - timedelta(days=1), date_to)
        start += step


def competition_path(competition: str | int) -> str:
    """``competition`` as a URL path segment.

    Raises:
        ValueError: not a competition code or id (guards the path against injection).
    """
    text = str(competition).strip()
    if not _COMPETITION.fullmatch(text) and not text.isdecimal():
        msg = f"not a competition code or id: {competition!r}"
        raise ValueError(msg)
    return text.upper()


def _match_records(payload: JsonValue) -> list[JsonValue] | None:
    """The ``matches`` list of a list-endpoint body, or ``None`` when it is not there."""
    if isinstance(payload, Mapping):
        records = payload.get("matches")
        if isinstance(records, list):
            return records
    return None


def validate_matches(records: list[JsonValue], *, path: str) -> list[Match]:
    """Validate each record; malformed ones are logged and skipped."""
    matches: list[Match] = []
    for index, raw in enumerate(records):
        try:
            matches.append(Match.model_validate(raw))
        except ValidationError as exc:
            logger.warning(
                "provider_record_skipped",
                provider=PROVIDER,
                endpoint=path,
                index=index,
                errors=[".".join(map(str, error["loc"])) for error in exc.errors()],
            )
    return matches


def create_football_data_client(
    api_key: str,
    *,
    config: ProviderConfig = FOOTBALL_DATA_ORG_CONFIG,
) -> tuple[HttpFootballDataClient, httpx.AsyncClient]:
    """Production wiring: the endpoint client and the HTTP client the caller must close.

    The key comes from ``Settings.require_secret("football_data_org_key")``; it is sent in the
    ``X-Auth-Token`` header only, never in a URL, so it cannot reach the logs.
    """
    http = create_http_client(config, api_key)
    api = ApiClient(config, http=http, quota=None, cache=TtlCache())
    return HttpFootballDataClient(api), http
