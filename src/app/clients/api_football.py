"""Typed endpoint client for API-Football v3 (api-sports.io).

Thin by design: one method per endpoint the system uses, returning validated
:mod:`app.clients.api_football_models` records. Mapping to domain objects lives in
``app.sports.football_mapping``. Request-saving rules (plan §9) that belong to the
endpoints themselves are applied here: ``ids=`` batches of at most 20 fixtures and
transparent pagination of ``odds``/``injuries``.

Every call states its :class:`~app.clients.quota.RequestPurpose`, so automatic jobs are
charged against the automatic part of the daily quota and user actions against all of it.
"""

from collections.abc import Sequence
from datetime import date
from itertools import batched
from typing import Final

import httpx
from pydantic import BaseModel, JsonValue, ValidationError

from app.clients.api_football_models import (
    Envelope,
    FixtureItem,
    InjuryItem,
    LineupItem,
    OddsItem,
    TeamStatisticsItem,
)
from app.clients.cache import QueryParams, TtlCache
from app.clients.http import ApiClient, ProviderConfig, create_http_client
from app.clients.quota import (
    DEFAULT_MANUAL_RESERVE_SHARE,
    QuotaPolicy,
    RequestPurpose,
    UsageStore,
    shared_guard,
)
from app.errors import ExternalServiceError
from app.logging import get_logger

logger = get_logger(__name__)

PROVIDER: Final = "api-football"
QUOTA_SPORT: Final = "football"  # API-Sports counts each sport API separately
FREE_PLAN_DAILY_LIMIT: Final = 100
FREE_PLAN_REQUESTS_PER_MINUTE: Final = 10
MAX_IDS_PER_REQUEST: Final = 20
# Safety stop for pagination (a league-day of odds is a handful of pages).
MAX_PAGES: Final = 30


def _rate_limited_body(payload: JsonValue) -> str | None:
    """Per-minute rate limit reported inside an HTTP 200 body: retry it like a 429."""
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, dict) and "rateLimit" in errors:
            return "rate limit reported in body"
    return None


API_FOOTBALL_CONFIG: Final = ProviderConfig(
    name=PROVIDER,
    base_url="https://v3.football.api-sports.io",
    auth_header="x-apisports-key",
    requests_per_minute=FREE_PLAN_REQUESTS_PER_MINUTE,
    limit_header="x-ratelimit-requests-limit",
    remaining_header="x-ratelimit-requests-remaining",
    retryable_body=_rate_limited_body,
)


class ApiFootballError(ExternalServiceError):
    """API-Football answered HTTP 200 with an ``errors`` object (bad key, plan, params)."""

    def __init__(self, endpoint: str, errors: dict[str, str]) -> None:
        """Keep the provider's error fields (``token``, ``plan``, ``rateLimit``...)."""
        self.errors = errors
        details = "; ".join(f"{field}: {message}" for field, message in sorted(errors.items()))
        super().__init__(f"{PROVIDER} {endpoint}: {details}")


class ApiFootballClient:
    """Endpoint methods over an :class:`~app.clients.http.ApiClient`."""

    def __init__(self, api: ApiClient) -> None:
        """Use ``api`` (already configured for API-Football) for every request."""
        self._api = api

    @property
    def api(self) -> ApiClient:
        """The underlying HTTP client (quota status, cache checks)."""
        return self._api

    # ------------------------------------------------------------------ fixtures

    async def fixtures_by_date(self, day: date, *, purpose: RequestPurpose) -> list[FixtureItem]:
        """All fixtures (every league) on ``day`` (UTC): 1 request."""
        return await self._records("fixtures", {"date": day.isoformat()}, FixtureItem, purpose)

    async def fixtures_by_league(
        self,
        league_id: int,
        season: int,
        *,
        purpose: RequestPurpose,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> list[FixtureItem]:
        """Fixtures of a league season, optionally within ``[date_from, date_to]``: 1 request."""
        params: dict[str, str | int] = {"league": league_id, "season": season}
        params.update(_date_range(date_from, date_to))
        return await self._records("fixtures", params, FixtureItem, purpose)

    async def fixtures_by_team(
        self,
        team_id: int,
        season: int,
        *,
        date_from: date,
        date_to: date,
        purpose: RequestPurpose,
    ) -> list[FixtureItem]:
        """Fixtures of one team (all competitions) in a date range: 1 request.

        ``from``/``to`` are only accepted together with ``season``.
        """
        params: dict[str, str | int] = {"team": team_id, "season": season}
        params.update(_date_range(date_from, date_to))
        return await self._records("fixtures", params, FixtureItem, purpose)

    async def fixtures_by_ids(
        self, fixture_ids: Sequence[int], *, purpose: RequestPurpose
    ) -> list[FixtureItem]:
        """Fixtures by id, ``ceil(n / 20)`` requests (``ids=a-b-c``)."""
        unique = list(dict.fromkeys(fixture_ids))
        items: list[FixtureItem] = []
        for chunk in batched(unique, MAX_IDS_PER_REQUEST, strict=False):
            ids = "-".join(str(fixture_id) for fixture_id in chunk)
            items.extend(await self._records("fixtures", {"ids": ids}, FixtureItem, purpose))
        return items

    # ------------------------------------------------------------------ odds

    async def odds_by_fixture(
        self, fixture_id: int, *, purpose: RequestPurpose, cache_ttl: float | None = None
    ) -> list[OddsItem]:
        """Pre-match odds of one fixture (all bookmakers): 1 request."""
        return await self._paged("odds", {"fixture": fixture_id}, OddsItem, purpose, cache_ttl)

    async def odds_by_league_date(
        self,
        league_id: int,
        season: int,
        day: date,
        *,
        purpose: RequestPurpose,
        cache_ttl: float | None = None,
    ) -> list[OddsItem]:
        """Odds of every fixture of a league on ``day``: one request per page."""
        params: dict[str, str | int] = {
            "league": league_id,
            "season": season,
            "date": day.isoformat(),
        }
        return await self._paged("odds", params, OddsItem, purpose, cache_ttl)

    # ------------------------------------------------------------------ context

    async def injuries_by_fixture(
        self, fixture_id: int, *, purpose: RequestPurpose
    ) -> list[InjuryItem]:
        """Missing/doubtful players of one fixture: 1 request."""
        return await self._paged("injuries", {"fixture": fixture_id}, InjuryItem, purpose, None)

    async def injuries_by_league(
        self, league_id: int, season: int, *, purpose: RequestPurpose
    ) -> list[InjuryItem]:
        """Missing/doubtful players of a whole league season: one request per page."""
        params: dict[str, str | int] = {"league": league_id, "season": season}
        return await self._paged("injuries", params, InjuryItem, purpose, None)

    async def lineups(self, fixture_id: int, *, purpose: RequestPurpose) -> list[LineupItem]:
        """Confirmed line-ups (empty until ~20-40 minutes before kick-off): 1 request."""
        return await self._records("fixtures/lineups", {"fixture": fixture_id}, LineupItem, purpose)

    async def fixture_statistics(
        self, fixture_id: int, *, purpose: RequestPurpose
    ) -> list[TeamStatisticsItem]:
        """Team statistics of a fixture, including ``expected_goals`` (xG): 1 request."""
        return await self._records(
            "fixtures/statistics", {"fixture": fixture_id}, TeamStatisticsItem, purpose
        )

    # ------------------------------------------------------------------ plumbing

    async def envelope(
        self,
        endpoint: str,
        params: QueryParams,
        *,
        purpose: RequestPurpose,
        cache_ttl: float | None = None,
    ) -> Envelope:
        """One raw (envelope-validated) response; used by the probe script and pagination.

        Raises:
            ApiFootballError: the provider reported errors in the body.
            ExternalServiceError: transport, HTTP or envelope validation failure.
        """
        response = await self._api.get_json(endpoint, params, purpose=purpose, cache_ttl=cache_ttl)
        try:
            envelope = Envelope.model_validate(response.payload)
        except ValidationError as exc:
            self._api.invalidate(endpoint, params)  # never replay a body we cannot read
            msg = f"{PROVIDER} {endpoint}: unexpected response format ({exc.error_count()} errors)"
            raise ExternalServiceError(msg) from exc
        envelope = envelope.model_copy(update={"fetched_at": response.fetched_at})
        if envelope.error_messages:
            # An error body (rate limit, plan, bad key) must not be served from cache.
            self._api.invalidate(endpoint, params)
            raise ApiFootballError(endpoint, envelope.error_messages)
        return envelope

    async def _records[M: BaseModel](
        self,
        endpoint: str,
        params: QueryParams,
        model: type[M],
        purpose: RequestPurpose,
        cache_ttl: float | None = None,
    ) -> list[M]:
        envelope = await self.envelope(endpoint, params, purpose=purpose, cache_ttl=cache_ttl)
        return validate_records(envelope, model, endpoint=endpoint)

    async def _paged[M: BaseModel](
        self,
        endpoint: str,
        params: QueryParams,
        model: type[M],
        purpose: RequestPurpose,
        cache_ttl: float | None,
    ) -> list[M]:
        records: list[M] = []
        page, total = 1, 1
        while page <= min(total, MAX_PAGES):
            paged = {**params, "page": page} if page > 1 else dict(params)
            envelope = await self.envelope(endpoint, paged, purpose=purpose, cache_ttl=cache_ttl)
            records.extend(validate_records(envelope, model, endpoint=endpoint))
            total = envelope.paging.total
            page += 1
        if total > MAX_PAGES:
            logger.warning("pagination_truncated", endpoint=endpoint, pages=total)
        return records


def validate_records[M: BaseModel](envelope: Envelope, model: type[M], *, endpoint: str) -> list[M]:
    """Validate each record of ``envelope``; malformed ones are logged and skipped.

    Models with a ``fetched_at`` field (odds) get the time the envelope was fetched, so a
    price served from cache keeps the time it was really taken.
    """
    records: list[M] = []
    stamped = envelope.fetched_at is not None and "fetched_at" in model.model_fields
    for index, raw in enumerate(envelope.records):
        try:
            record = model.model_validate(raw)
            if stamped:
                record = record.model_copy(update={"fetched_at": envelope.fetched_at})
            records.append(record)
        except ValidationError as exc:
            logger.warning(
                "provider_record_skipped",
                provider=PROVIDER,
                endpoint=endpoint,
                index=index,
                errors=[".".join(map(str, error["loc"])) for error in exc.errors()],
            )
    return records


def _date_range(date_from: date | None, date_to: date | None) -> dict[str, str]:
    params: dict[str, str] = {}
    if date_from is not None:
        params["from"] = date_from.isoformat()
    if date_to is not None:
        params["to"] = date_to.isoformat()
    return params


def create_api_football_client(
    api_key: str,
    store: UsageStore,
    *,
    daily_limit: int = FREE_PLAN_DAILY_LIMIT,
    manual_reserve_share: float = DEFAULT_MANUAL_RESERVE_SHARE,
    config: ProviderConfig = API_FOOTBALL_CONFIG,
) -> tuple[ApiFootballClient, httpx.AsyncClient]:
    """Production wiring: the endpoint client and the HTTP client the caller must close.

    Clients over the same ``store`` share one quota guard, so several clients (bot actions,
    scheduler, probe) can never overspend the daily limit between them.
    """
    http = create_http_client(config, api_key)
    quota = shared_guard(
        store,
        QuotaPolicy(daily_limit=daily_limit, manual_reserve_share=manual_reserve_share),
        provider=PROVIDER,
        sport=QUOTA_SPORT,
    )
    api = ApiClient(config, http=http, quota=quota, cache=TtlCache())
    return ApiFootballClient(api), http
