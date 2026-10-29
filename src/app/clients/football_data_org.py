"""Football-Data.org v4 API service (``https://api.football-data.org/v4``).

One method per documented endpoint, typed both ways: requests are the frozen filters of
:mod:`app.clients.football_data_org_requests`, answers the models of
:mod:`app.clients.football_data_org_models`. Rate limit, retry with backoff on 429/5xx (honouring
``Retry-After``), the TTL cache, single-flight and the call log all come from
:class:`~app.clients.http.ApiClient`; this module only knows the endpoints.

Provider rules applied here (v4 reference, ``docs.football-data.org/general/v4``):

- authentication: the ``X-Auth-Token`` header (never a URL parameter, so never logged);
- throttling per minute only, no daily limit: free plan 10, Standard 30, higher plans 60
  requests a minute (``FOOTBALL_DATA_REQUESTS_PER_MINUTE``); no daily ``QuotaGuard``;
- errors: ``400`` malformed filter, ``403`` resource outside the plan, ``404`` unknown
  resource, ``429`` too many requests; the body carries ``error`` / ``message``;
- ``/matches`` takes a date range of at most 10 days: longer ranges are split into windows;
- path segments (competition codes, ids) are validated before they become part of a URL;
- line-ups, goals, bookings and odds come with ``GET /matches/{id}`` where the plan has them.
"""

import re
from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import date, timedelta
from typing import Final, Protocol

import httpx
from pydantic import BaseModel, JsonValue, ValidationError

from app.clients.cache import QueryParams, TtlCache
from app.clients.football_data_org_models import (
    AreaDetail,
    AreaList,
    CompetitionDetail,
    CompetitionList,
    Head2Head,
    Head2HeadWrapper,
    Match,
    MatchLineups,
    PersonDetail,
    Scorers,
    Standings,
    Team,
    TeamList,
    match_lineups,
)
from app.clients.football_data_org_requests import (
    CompetitionListFilters,
    CompetitionMatchFilters,
    CompetitionTeamsFilters,
    Head2HeadFilters,
    MatchFilters,
    PersonMatchFilters,
    ScorersFilters,
    StandingsFilters,
    TeamMatchFilters,
)
from app.clients.http import ApiClient, ProviderConfig, create_http_client
from app.clients.quota import QuotaGuard, QuotaPolicy, RequestPurpose, UsageStore, shared_guard
from app.domain import CallRecorder, Provider
from app.errors import ExternalServiceError
from app.logging import get_logger

logger = get_logger(__name__)

PROVIDER: Final = "football-data-org"
QUOTA_SPORT: Final = "football"
MINUTES_PER_DAY: Final = 24 * 60
FREE_PLAN_REQUESTS_PER_MINUTE: Final = 10
STANDARD_PLAN_REQUESTS_PER_MINUTE: Final = 30
MAX_DAYS_PER_MATCHES_REQUEST: Final = 10
"""``/matches`` rejects a ``dateFrom``..``dateTo`` range longer than this (both days count)."""
TOP_COMPETITIONS: Final = ("PL", "CL", "PD", "BL1", "SA", "FL1")
"""England, Champions League, Spain, Germany, Italy, France (all on the free plan)."""
MATCH_DETAIL_TTL_SECONDS: Final = 5 * 60.0
"""Line-ups appear shortly before kick-off, so a match body is not kept as long as a schedule."""
REFERENCE_TTL_SECONDS: Final = 24 * 60 * 60.0
"""Areas and competitions change a few times a season."""
TEAM_TTL_SECONDS: Final = 6 * 60 * 60.0
FRESH_TTL_SECONDS: Final = 0.0
"""Asks the provider every time (the cache is neither read nor written)."""

# 2-5 letters/digits (``PL``, ``BL1``) or a numeric id (``2021``); nothing that could alter a path.
_COMPETITION: Final = re.compile(r"[A-Za-z0-9]{2,5}")

FOOTBALL_DATA_ORG_CONFIG: Final = ProviderConfig(
    name=PROVIDER,
    base_url="https://api.football-data.org/v4/",
    auth_header="X-Auth-Token",
    requests_per_minute=FREE_PLAN_REQUESTS_PER_MINUTE,
    log_provider=Provider.FOOTBALL_DATA,
)


class FootballDataClient(Protocol):
    """The Football-Data.org API as the system sees it; tests and fakes implement this."""

    async def areas(self, *, purpose: RequestPurpose) -> AreaList:
        """``GET /areas``."""
        ...

    async def area(self, area_id: int, *, purpose: RequestPurpose) -> AreaDetail:
        """``GET /areas/{id}``."""
        ...

    async def competitions(
        self, filters: CompetitionListFilters, *, purpose: RequestPurpose
    ) -> CompetitionList:
        """``GET /competitions``."""
        ...

    async def competition(
        self, competition: str | int, *, purpose: RequestPurpose
    ) -> CompetitionDetail:
        """``GET /competitions/{code}``."""
        ...

    async def competition_matches(
        self,
        competition: str | int,
        filters: CompetitionMatchFilters,
        *,
        purpose: RequestPurpose,
    ) -> list[Match]:
        """``GET /competitions/{code}/matches``."""
        ...

    async def standings(
        self, competition: str | int, filters: StandingsFilters, *, purpose: RequestPurpose
    ) -> Standings:
        """``GET /competitions/{code}/standings``."""
        ...

    async def scorers(
        self, competition: str | int, filters: ScorersFilters, *, purpose: RequestPurpose
    ) -> Scorers:
        """``GET /competitions/{code}/scorers``."""
        ...

    async def competition_teams(
        self,
        competition: str | int,
        filters: CompetitionTeamsFilters,
        *,
        purpose: RequestPurpose,
    ) -> TeamList:
        """``GET /competitions/{code}/teams``."""
        ...

    async def team(self, team_id: int, *, purpose: RequestPurpose) -> Team:
        """``GET /teams/{id}``."""
        ...

    async def team_matches(
        self, team_id: int, filters: TeamMatchFilters, *, purpose: RequestPurpose
    ) -> list[Match]:
        """``GET /teams/{id}/matches``."""
        ...

    async def person(self, person_id: int, *, purpose: RequestPurpose) -> PersonDetail:
        """``GET /persons/{id}``."""
        ...

    async def person_matches(
        self, person_id: int, filters: PersonMatchFilters, *, purpose: RequestPurpose
    ) -> list[Match]:
        """``GET /persons/{id}/matches``."""
        ...

    async def matches(
        self, filters: MatchFilters, *, purpose: RequestPurpose, fresh: bool = False
    ) -> list[Match]:
        """``GET /matches`` (``fresh``: never answered from the cache)."""
        ...

    async def matches_between(
        self, date_from: date, date_to: date, *, purpose: RequestPurpose
    ) -> list[Match]:
        """Matches with kick-off in ``date_from``..``date_to`` (UTC days, both included)."""
        ...

    async def match(self, match_id: int, *, purpose: RequestPurpose) -> Match:
        """``GET /matches/{id}``."""
        ...

    async def lineups(self, match_id: int, *, purpose: RequestPurpose) -> MatchLineups:
        """Starting elevens, benches and coaches of both sides of one match."""
        ...

    async def head2head(
        self, match_id: int, filters: Head2HeadFilters, *, purpose: RequestPurpose
    ) -> Head2Head:
        """``GET /matches/{id}/head2head``."""
        ...


class HttpFootballDataClient:
    """:class:`FootballDataClient` over an :class:`~app.clients.http.ApiClient`.

    Every method costs one request (cache answers cost none) unless its docstring says more.

    Raises (every method):
        ProviderHttpError: ``400``/``403``/``404`` (``status_code`` tells which).
        ProviderUnavailableError: 429/5xx/timeouts persisted through every retry.
        ExternalServiceError: the answer is not the documented shape (it is not cached).
    """

    def __init__(self, api: ApiClient) -> None:
        """Wrap the provider-level client (rate limit, retry, cache, call log)."""
        self._api = api

    # ------------------------------------------------------------------- areas, competitions
    async def areas(self, *, purpose: RequestPurpose) -> AreaList:
        """Every area (country, continent)."""
        return await self._object("areas", {}, AreaList, purpose, REFERENCE_TTL_SECONDS)

    async def area(self, area_id: int, *, purpose: RequestPurpose) -> AreaDetail:
        """One area with its parent and children."""
        return await self._object(
            f"areas/{_id(area_id)}", {}, AreaDetail, purpose, REFERENCE_TTL_SECONDS
        )

    async def competitions(
        self, filters: CompetitionListFilters, *, purpose: RequestPurpose
    ) -> CompetitionList:
        """Competitions the key may read (with their current season)."""
        return await self._object(
            "competitions", filters.params(), CompetitionList, purpose, REFERENCE_TTL_SECONDS
        )

    async def competition(
        self, competition: str | int, *, purpose: RequestPurpose
    ) -> CompetitionDetail:
        """One competition with every season the provider has."""
        path = f"competitions/{competition_path(competition)}"
        return await self._object(path, {}, CompetitionDetail, purpose, REFERENCE_TTL_SECONDS)

    async def competition_matches(
        self,
        competition: str | int,
        filters: CompetitionMatchFilters,
        *,
        purpose: RequestPurpose,
    ) -> list[Match]:
        """Matches of one competition (a whole season when only ``season`` is given)."""
        path = f"competitions/{competition_path(competition)}/matches"
        return await self._matches(path, filters.params(), purpose)

    async def standings(
        self, competition: str | int, filters: StandingsFilters, *, purpose: RequestPurpose
    ) -> Standings:
        """League tables (total, home, away; one per group in group stages)."""
        path = f"competitions/{competition_path(competition)}/standings"
        return await self._object(path, filters.params(), Standings, purpose)

    async def scorers(
        self, competition: str | int, filters: ScorersFilters, *, purpose: RequestPurpose
    ) -> Scorers:
        """Top scorers (the provider's default limit is 10)."""
        path = f"competitions/{competition_path(competition)}/scorers"
        return await self._object(path, filters.params(), Scorers, purpose)

    async def competition_teams(
        self,
        competition: str | int,
        filters: CompetitionTeamsFilters,
        *,
        purpose: RequestPurpose,
    ) -> TeamList:
        """Teams of a competition season, with squads."""
        path = f"competitions/{competition_path(competition)}/teams"
        return await self._object(path, filters.params(), TeamList, purpose, TEAM_TTL_SECONDS)

    # ----------------------------------------------------------------------- teams, persons
    async def team(self, team_id: int, *, purpose: RequestPurpose) -> Team:
        """One team with coach, squad and running competitions."""
        return await self._object(f"teams/{_id(team_id)}", {}, Team, purpose, TEAM_TTL_SECONDS)

    async def team_matches(
        self, team_id: int, filters: TeamMatchFilters, *, purpose: RequestPurpose
    ) -> list[Match]:
        """Matches of one team across competitions."""
        return await self._matches(f"teams/{_id(team_id)}/matches", filters.params(), purpose)

    async def person(self, person_id: int, *, purpose: RequestPurpose) -> PersonDetail:
        """One player or coach."""
        return await self._object(
            f"persons/{_id(person_id)}", {}, PersonDetail, purpose, TEAM_TTL_SECONDS
        )

    async def person_matches(
        self, person_id: int, filters: PersonMatchFilters, *, purpose: RequestPurpose
    ) -> list[Match]:
        """Matches a person took part in."""
        path = f"persons/{_id(person_id)}/matches"
        return await self._matches(path, filters.params(), purpose)

    # ------------------------------------------------------------------------------ matches
    async def matches(
        self, filters: MatchFilters, *, purpose: RequestPurpose, fresh: bool = False
    ) -> list[Match]:
        """Matches of every competition of the plan.

        A range over 10 days costs one request per 10 days (windows asked in order).
        ``fresh`` skips the cache: results and closing prices must be the provider's latest.
        """
        ttl = FRESH_TTL_SECONDS if fresh else None
        if filters.date_from is None or filters.date_to is None:
            return await self._matches("matches", filters.params(), purpose, ttl)
        found: list[Match] = []
        for window_from, window_to in date_windows(filters.date_from, filters.date_to):
            window = MatchFilters(
                ids=filters.ids,
                date_from=window_from,
                date_to=window_to,
                status=filters.status,
                competitions=filters.competitions,
            )
            found.extend(await self._matches("matches", window.params(), purpose, ttl))
        return found

    async def matches_between(
        self, date_from: date, date_to: date, *, purpose: RequestPurpose
    ) -> list[Match]:
        """Every match of the UTC days ``date_from``..``date_to``.

        Raises:
            ValueError: ``date_to`` is before ``date_from``.
        """
        return await self.matches(
            MatchFilters(date_from=date_from, date_to=date_to), purpose=purpose
        )

    async def match(self, match_id: int, *, purpose: RequestPurpose) -> Match:
        """One match in full (line-ups, goals, bookings, odds where the plan has them)."""
        return await self._object(
            f"matches/{_id(match_id)}", {}, Match, purpose, MATCH_DETAIL_TTL_SECONDS
        )

    async def lineups(self, match_id: int, *, purpose: RequestPurpose) -> MatchLineups:
        """Both sides' line-ups of a match (empty lists until they are announced)."""
        return match_lineups(await self.match(match_id, purpose=purpose))

    async def head2head(
        self, match_id: int, filters: Head2HeadFilters, *, purpose: RequestPurpose
    ) -> Head2Head:
        """Earlier meetings of the two teams of a match, with totals."""
        path = f"matches/{_id(match_id)}/head2head"
        params = filters.params()
        wrapper = await self._object(path, params, Head2HeadWrapper, purpose)
        return Head2Head(
            aggregates=wrapper.aggregates,
            matches=tuple(validate_matches(list(wrapper.matches), path=path)),
        )

    # ---------------------------------------------------------------------------- plumbing
    async def _object[M: BaseModel](
        self,
        path: str,
        params: QueryParams,
        model: type[M],
        purpose: RequestPurpose,
        cache_ttl: float | None = None,
    ) -> M:
        response = await self._api.get_json(path, params, purpose=purpose, cache_ttl=cache_ttl)
        try:
            return model.model_validate(response.payload)
        except ValidationError as exc:
            self._api.invalidate(path, params)  # never replay a body we cannot read
            msg = f"{PROVIDER} {path}: unexpected response format ({exc.error_count()} errors)"
            raise ExternalServiceError(msg) from exc

    async def _matches(
        self,
        path: str,
        params: QueryParams,
        purpose: RequestPurpose,
        cache_ttl: float | None = None,
    ) -> list[Match]:
        response = await self._api.get_json(path, params, purpose=purpose, cache_ttl=cache_ttl)
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


def _id(value: int) -> int:
    """A resource id for a URL path (``bool`` and negatives are refused)."""
    # Typed as int, but a bool is an int too and the id may come from untrusted input.
    if isinstance(value, bool) or value < 1:
        msg = f"not a resource id: {value!r}"
        raise ValueError(msg)
    return value


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
    requests_per_minute: int = FREE_PLAN_REQUESTS_PER_MINUTE,
    config: ProviderConfig = FOOTBALL_DATA_ORG_CONFIG,
    recorder: CallRecorder | None = None,
    quota: QuotaGuard | None = None,
) -> tuple[HttpFootballDataClient, httpx.AsyncClient]:
    """Production wiring: the API service and the HTTP client the caller must close.

    The key comes from ``Settings.require_secret("football_data_org_key")``; it is sent in the
    ``X-Auth-Token`` header only, never in a URL, so it cannot reach the logs.
    ``requests_per_minute`` is the plan's limit (free 10, Standard 30). ``recorder`` stores
    every request in the call log; ``quota`` (see :func:`usage_counter`) counts them per day.
    """
    paced = replace(config, requests_per_minute=requests_per_minute)
    http = create_http_client(paced, api_key)
    api = ApiClient(paced, http=http, quota=quota, cache=TtlCache(), recorder=recorder)
    return HttpFootballDataClient(api), http


def usage_counter(store: UsageStore, requests_per_minute: int) -> QuotaGuard:
    """A daily request counter that never refuses (the provider has no daily limit).

    Its ceiling is every request the per-minute pacing can send in a day, so the counts in
    ``store`` (``api_usage``) show what was spent without ever blocking a call.
    """
    ceiling = requests_per_minute * MINUTES_PER_DAY
    return shared_guard(
        store,
        QuotaPolicy(daily_limit=ceiling, manual_reserve_share=0.0),
        provider=PROVIDER,
        sport=QUOTA_SPORT,
    )
