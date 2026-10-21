"""The ``SportAdapter`` contract: how the pipeline obtains data for one sport.

Frozen contract (``docs/contracts.md`` §2). An adapter only *fetches and maps* provider
data into domain objects; it never writes to the database (the pipeline persists what it
returns) and never predicts (statistical models live in ``app.models``).
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from app.domain import (
    DomainValidationError,
    ExternalRef,
    Fixture,
    FixtureResult,
    HistoryBatch,
    MarketType,
    OddsKind,
    OddsSnapshot,
    Sport,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class FixtureQuery:
    """Which fixtures to fetch: an inclusive UTC date range, optionally narrowed.

    Empty ``leagues`` and ``teams`` mean "every league the adapter follows". When both are
    given, a fixture matches if it belongs to one of the leagues **or** involves one of the
    teams (the bot's scope is a union of the user's picks).
    """

    date_from: date
    date_to: date
    leagues: frozenset[ExternalRef] = frozenset()
    teams: frozenset[ExternalRef] = frozenset()

    def __post_init__(self) -> None:
        """Reject an inverted date range."""
        if self.date_to < self.date_from:
            msg = f"date_to {self.date_to} is before date_from {self.date_from}"
            raise DomainValidationError(msg)


class SportAdapter(Protocol):
    """Data access for one sport, independent of the provider behind it.

    Failure contract: provider/network failures raise
    :class:`~app.errors.ExternalServiceError`; a single malformed provider record is logged
    and skipped, never fails the whole call. Every method is safe to call repeatedly
    (results are values; persistence is idempotent upstream).
    """

    @property
    def sport(self) -> Sport:
        """The sport this adapter serves."""
        ...

    def supported_markets(self) -> frozenset[MarketType]:
        """Market types for which :meth:`fetch_odds` can return prices."""
        ...

    async def fetch_fixtures(self, query: FixtureQuery) -> list[Fixture]:
        """Fixtures whose kickoff (UTC date) falls in the query range and scope."""
        ...

    async def fetch_odds(
        self, fixtures: Sequence[ExternalRef], *, kind: OddsKind
    ) -> list[OddsSnapshot]:
        """Current prices for ``fixtures`` in the supported markets, tagged with ``kind``."""
        ...

    async def fetch_results(self, fixtures: Sequence[ExternalRef]) -> list[FixtureResult]:
        """Results for ``fixtures``; fixtures that are not over yet are omitted."""
        ...

    async def refresh_history(self, leagues: Collection[ExternalRef], season: int) -> HistoryBatch:
        """All fixtures of ``season`` in ``leagues`` with known results (and odds if any)."""
        ...
