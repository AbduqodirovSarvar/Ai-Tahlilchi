"""The pipeline's view of a data provider: an adapter, its quota and a few extras.

The football implementation wraps :class:`~app.sports.football.FootballAdapter` over the
API-Football client. Failures are translated at this boundary (see
:mod:`app.pipeline.errors`), so nothing above the pipeline imports ``app.clients``.
"""

from collections.abc import Awaitable, Collection, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from app.clients.api_football import PROVIDER as API_FOOTBALL
from app.clients.api_football import ApiFootballClient
from app.clients.quota import QuotaExceededError, RequestPurpose
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
from app.sports.football import FootballAdapter
from app.sports.football_leagues import LeagueCatalog
from app.sports.football_mapping import SOURCE, SUPPORTED_MARKETS

type Purpose = RequestPurpose
"""Who asks: ``AUTOMATIC`` (scheduled jobs) or ``MANUAL`` (the user's actions)."""
SUPPORTED_SPORT_MARKETS: Final[dict[Sport, frozenset[MarketType]]] = {
    Sport.FOOTBALL: SUPPORTED_MARKETS
}
"""Markets each implemented sport can price, known without a configured provider."""
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
        """Source name of the provider's own fixtures (``api-football``)."""
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
        self, results: Sequence[FixtureResult], *, purpose: Purpose
    ) -> list[FixtureResult]:
        """``results`` with expected goals added where the provider has them."""
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


class FootballProvider:
    """API-Football behind the football adapter."""

    def __init__(
        self,
        client: ApiFootballClient,
        *,
        catalog: LeagueCatalog | None = None,
        clock: Clock = system_clock,
    ) -> None:
        """Serve the tracked leagues of ``catalog`` through ``client``."""
        self._client = client
        self._base = FootballAdapter(client, catalog=catalog, clock=clock)

    @property
    def sport(self) -> Sport:
        """Football."""
        return Sport.FOOTBALL

    @property
    def source(self) -> str:
        """``api-football``."""
        return SOURCE

    def markets(self) -> frozenset[MarketType]:
        """Markets the football adapter prices."""
        return self._base.supported_markets()

    def adapter(self, purpose: Purpose) -> SportAdapter:
        """The football adapter charging ``purpose``, with errors translated."""
        return GuardedAdapter(self._base.for_purpose(purpose), API_FOOTBALL)

    async def quota(self) -> QuotaSnapshot:
        """Today's API-Football usage."""
        status = await self._client.api.quota.status()
        return QuotaSnapshot(
            provider=API_FOOTBALL,
            sport=Sport.FOOTBALL,
            used=status.counts.requests_used,
            limit=status.policy.daily_limit,
            manual_used=status.counts.manual_used,
            remaining=status.remaining,
            automatic_available=status.automatic_available,
        )

    async def enrich_results(
        self, results: Sequence[FixtureResult], *, purpose: Purpose
    ) -> list[FixtureResult]:
        """Add expected goals to finished results (one request each)."""
        adapter = self._base.for_purpose(purpose)
        return await _guarded(adapter.with_expected_goals(results), API_FOOTBALL)

    async def lineups_confirmed(self, fixture: ExternalRef, *, purpose: Purpose) -> bool:
        """True when API-Football lists line-ups for ``fixture``."""
        adapter = self._base.for_purpose(purpose)
        return bool(await _guarded(adapter.fetch_lineups(fixture), API_FOOTBALL))
