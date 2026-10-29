"""The pipeline's view of a data provider: an adapter, its usage and a few extras.

The football implementation wraps :class:`~app.sports.football_data_org.FootballDataAdapter`
over the Football-Data.org client (ADR 0025). Failures are translated at this boundary (see
:mod:`app.pipeline.errors`), so nothing above the pipeline imports ``app.clients``.
"""

from collections.abc import Awaitable, Collection, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from app.clients.football_data_org import PROVIDER as FOOTBALL_DATA
from app.clients.football_data_org import FootballDataClient
from app.clients.quota import (
    DEFAULT_MANUAL_RESERVE_SHARE,
    QuotaExceededError,
    QuotaGuard,
    RequestPurpose,
)
from app.domain import (
    ExternalRef,
    Fixture,
    FixtureResult,
    HistoryBatch,
    MarketType,
    OddsKind,
    OddsSnapshot,
    Sport,
)
from app.errors import ExternalServiceError
from app.pipeline.clock import Clock, system_clock
from app.pipeline.errors import BudgetExhaustedError, ProviderUnavailableError
from app.sports.base import FixtureQuery, SportAdapter
from app.sports.football_data_org import SOURCE, SUPPORTED_MARKETS, FootballDataAdapter
from app.sports.football_leagues import LeagueCatalog

type Purpose = RequestPurpose
"""Who asks: ``AUTOMATIC`` (scheduled jobs) or ``MANUAL`` (the user's actions)."""
SUPPORTED_SPORT_MARKETS: Final[dict[Sport, frozenset[MarketType]]] = {
    Sport.FOOTBALL: frozenset(MarketType)
}
"""Markets each implemented sport can price, known without a configured provider."""
DEFAULT_DAILY_LIMIT: Final = 100
"""The provider's free-plan daily request limit, shown when no provider is configured."""
DEFAULT_MANUAL_RESERVE: Final = round(DEFAULT_DAILY_LIMIT * DEFAULT_MANUAL_RESERVE_SHARE)
"""Requests of that limit kept back for the user's own actions."""
AUTOMATIC: Final = RequestPurpose.AUTOMATIC
MANUAL: Final = RequestPurpose.MANUAL


@dataclass(frozen=True, slots=True, kw_only=True)
class QuotaSnapshot:
    """Today's provider usage and what each purpose may still spend."""

    provider: str
    sport: Sport | None
    used: int
    limit: int
    manual_used: int
    remaining: int
    automatic_available: int
    manual_reserve: int = 0

    def available(self, purpose: Purpose) -> int:
        """Requests ``purpose`` may still spend today."""
        return self.remaining if purpose is MANUAL else self.automatic_available


class SportProvider(Protocol):
    """Everything the pipeline needs from one sport's data source."""

    @property
    def sport(self) -> Sport:
        """The sport served."""
        ...

    @property
    def source(self) -> str:
        """Source name of the provider's own fixtures (``ExternalRef.source``)."""
        ...

    def markets(self) -> frozenset[MarketType]:
        """Market types the provider can price."""
        ...

    def adapter(self, purpose: Purpose) -> SportAdapter:
        """A :class:`SportAdapter` whose requests are charged to ``purpose``."""
        ...

    async def quota(self) -> QuotaSnapshot:
        """Today's usage."""
        ...

    async def enrich_results(
        self, results: Sequence[FixtureResult], *, purpose: Purpose, max_requests: int
    ) -> list[FixtureResult]:
        """``results`` with expected goals added where the provider has them (capped)."""
        ...

    async def lineups_confirmed(self, fixture: ExternalRef, *, purpose: Purpose) -> bool:
        """True once the provider has published the line-ups of ``fixture``."""
        ...


async def _guarded[T](call: Awaitable[T], provider: str) -> T:
    """Await ``call``, translating provider failures into pipeline errors."""
    try:
        return await call
    except QuotaExceededError as error:
        raise BudgetExhaustedError(error.needed, error.available) from error
    except ExternalServiceError as error:
        raise ProviderUnavailableError(provider, str(error)) from error


class GuardedAdapter:
    """A :class:`SportAdapter` that raises pipeline errors instead of client errors."""

    def __init__(self, inner: SportAdapter, provider: str) -> None:
        """Wrap ``inner``; ``provider`` names it in errors."""
        self._inner = inner
        self._provider = provider

    @property
    def sport(self) -> Sport:
        """The wrapped adapter's sport."""
        return self._inner.sport

    def supported_markets(self) -> frozenset[MarketType]:
        """The wrapped adapter's markets."""
        return self._inner.supported_markets()

    async def fetch_fixtures(self, query: FixtureQuery) -> list[Fixture]:
        """See :meth:`SportAdapter.fetch_fixtures`."""
        return await _guarded(self._inner.fetch_fixtures(query), self._provider)

    async def fetch_odds(
        self, fixtures: Sequence[ExternalRef], *, kind: OddsKind
    ) -> list[OddsSnapshot]:
        """See :meth:`SportAdapter.fetch_odds`."""
        return await _guarded(self._inner.fetch_odds(fixtures, kind=kind), self._provider)

    async def fetch_results(self, fixtures: Sequence[ExternalRef]) -> list[FixtureResult]:
        """See :meth:`SportAdapter.fetch_results`."""
        return await _guarded(self._inner.fetch_results(fixtures), self._provider)

    async def refresh_history(self, leagues: Collection[ExternalRef], season: int) -> HistoryBatch:
        """See :meth:`SportAdapter.refresh_history`."""
        return await _guarded(self._inner.refresh_history(leagues, season), self._provider)


class FootballDataProvider:
    """Football-Data.org behind the football adapter (fixtures, results, history, 1X2 odds).

    ``quota`` counts the requests of each day in ``api_usage`` (the provider has no daily
    limit: its ceiling is what the per-minute pacing allows, so it never refuses a request).
    """

    def __init__(
        self,
        client: FootballDataClient,
        *,
        quota: QuotaGuard,
        catalog: LeagueCatalog | None = None,
        clock: Clock = system_clock,
    ) -> None:
        """Serve the tracked leagues of ``catalog`` through ``client``."""
        self._quota = quota
        self._base = FootballDataAdapter(client, catalog=catalog, clock=clock)

    @property
    def sport(self) -> Sport:
        """Football."""
        return Sport.FOOTBALL

    @property
    def source(self) -> str:
        """``football-data-org``."""
        return SOURCE

    def markets(self) -> frozenset[MarketType]:
        """1X2 (the only market the provider prices)."""
        return SUPPORTED_MARKETS

    def adapter(self, purpose: Purpose) -> SportAdapter:
        """The football adapter acting for ``purpose``, with errors translated."""
        return GuardedAdapter(self._base.for_purpose(purpose), FOOTBALL_DATA)

    async def quota(self) -> QuotaSnapshot:
        """Today's Football-Data.org requests (against the per-minute ceiling of the day)."""
        status = await self._quota.status()
        return QuotaSnapshot(
            provider=FOOTBALL_DATA,
            sport=Sport.FOOTBALL,
            used=status.counts.requests_used,
            limit=status.policy.daily_limit,
            manual_used=status.counts.manual_used,
            remaining=status.remaining,
            automatic_available=status.automatic_available,
            manual_reserve=status.policy.manual_reserve,
        )

    async def enrich_results(
        self,
        results: Sequence[FixtureResult],
        *,
        purpose: Purpose,  # noqa: ARG002 - port signature
        max_requests: int,  # noqa: ARG002 - port signature
    ) -> list[FixtureResult]:
        """``results`` unchanged: the provider publishes no expected goals."""
        return list(results)

    async def lineups_confirmed(self, fixture: ExternalRef, *, purpose: Purpose) -> bool:
        """True when Football-Data.org lists both starting elevens of ``fixture``."""
        adapter = self._base.for_purpose(purpose)
        return await _guarded(adapter.lineups_confirmed(fixture), FOOTBALL_DATA)
