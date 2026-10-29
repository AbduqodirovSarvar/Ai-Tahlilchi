"""A scripted odds provider: serves the test world through the pipeline's provider port."""

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from app.domain import (
    ExternalRef,
    Fixture,
    FixtureResult,
    FixtureStatus,
    HistoryBatch,
    League,
    MarketType,
    OddsKind,
    OddsSnapshot,
    Sport,
    Team,
)
from app.errors import ExternalServiceError
from app.pipeline.errors import BudgetExhaustedError, ProviderUnavailableError
from app.pipeline.provider import MANUAL, Purpose, QuotaSnapshot
from app.sports.base import FixtureQuery
from tests.pipeline.world import START, World
from tests.services.contract_suite import MutableClock

API_LEAGUES = {"E0": "39", "SP1": "140"}
# The source the stored test world was written under (league ids keep that numbering).
SOURCE = "api-football"
SUPPORTED_MARKETS = frozenset(MarketType)


def _api_fixture(fixture: Fixture, number: int) -> Fixture:
    code = fixture.league.ref.external_id
    league = League(
        ref=ExternalRef(SOURCE, API_LEAGUES[code]),
        sport=Sport.FOOTBALL,
        name=fixture.league.name,
        country=fixture.league.country,
    )
    return replace(
        fixture,
        ref=ExternalRef(SOURCE, str(9000 + number)),
        league=league,
        home=Team(
            ref=ExternalRef(SOURCE, f"t-{fixture.home.name}"),
            sport=Sport.FOOTBALL,
            name=fixture.home.name,
        ),
        away=Team(
            ref=ExternalRef(SOURCE, f"t-{fixture.away.name}"),
            sport=Sport.FOOTBALL,
            name=fixture.away.name,
        ),
    )


@dataclass
class ScriptedAdapter:
    """Records its calls and answers from the world; failure modes are switchable."""

    provider: "ScriptedProvider"
    purpose: Purpose

    @property
    def sport(self) -> Sport:
        """Football."""
        return Sport.FOOTBALL

    def supported_markets(self) -> frozenset[MarketType]:
        """Everything the football adapter prices."""
        return SUPPORTED_MARKETS

    def _check(self, call: str) -> None:
        self.provider.calls.append((call, self.purpose))
        self.provider.used += 1
        if self.provider.fail_with is not None:
            raise self.provider.fail_with
        if self.provider.exhausted:
            raise BudgetExhaustedError(5, 0)

    async def fetch_fixtures(self, query: FixtureQuery) -> list[Fixture]:
        """Fixtures whose kick-off day is inside the query."""
        self._check("fixtures")
        self.provider.queries.append(query)
        return [
            f
            for f in self.provider.fixtures
            if query.date_from <= f.kickoff.date() <= query.date_to
        ]

    async def fetch_odds(
        self, fixtures: Sequence[ExternalRef], *, kind: OddsKind
    ) -> list[OddsSnapshot]:
        """Current prices, stamped with the test clock."""
        self._check("odds")
        wanted = set(fixtures)
        now = self.provider.clock()
        return [
            replace(snapshot, kind=kind, taken_at=now)
            for snapshot in self.provider.odds
            if snapshot.fixture in wanted
        ]

    async def fetch_results(self, fixtures: Sequence[ExternalRef]) -> list[FixtureResult]:
        """Results of matches that ended before the test clock."""
        self._check("results")
        wanted = set(fixtures)
        now = self.provider.clock()
        return [
            self.provider.results[ref]
            for ref in wanted
            if ref in self.provider.results
            and self.provider.kickoffs[ref] + timedelta(hours=2) <= now
        ]

    async def refresh_history(self, leagues: Collection[ExternalRef], season: int) -> HistoryBatch:  # noqa: ARG002
        """Not used by the pipeline."""
        return HistoryBatch()


@dataclass
class ScriptedProvider:
    """A :class:`~app.pipeline.provider.SportProvider` built from the test world."""

    clock: MutableClock
    fixtures: list[Fixture]
    odds: list[OddsSnapshot]
    results: dict[ExternalRef, FixtureResult]
    kickoffs: dict[ExternalRef, datetime]
    calls: list[tuple[str, Purpose]] = field(default_factory=list)
    queries: list[FixtureQuery] = field(default_factory=list)
    fail_with: ExternalServiceError | ProviderUnavailableError | None = None
    exhausted: bool = False
    lineups: bool = False
    used: int = 0
    limit: int = 100

    @property
    def sport(self) -> Sport:
        """Football."""
        return Sport.FOOTBALL

    @property
    def source(self) -> str:
        """The provider's fixture source."""
        return SOURCE

    def markets(self) -> frozenset[MarketType]:
        """Everything priced."""
        return SUPPORTED_MARKETS

    def adapter(self, purpose: Purpose) -> ScriptedAdapter:
        """An adapter charging ``purpose``."""
        return ScriptedAdapter(self, purpose)

    async def quota(self) -> QuotaSnapshot:
        """The scripted quota (``exhausted`` empties it)."""
        remaining = 0 if self.exhausted else self.limit - self.used
        return QuotaSnapshot(
            provider="api-football",
            sport=Sport.FOOTBALL,
            used=self.used,
            limit=self.limit,
            manual_used=0,
            remaining=remaining,
            automatic_available=max(remaining - 30, 0),
        )

    async def enrich_results(
        self,
        results: Sequence[FixtureResult],
        *,
        purpose: Purpose,  # noqa: ARG002
        max_requests: int,  # noqa: ARG002
    ) -> list[FixtureResult]:
        """No xG in the script."""
        return list(results)

    async def lineups_confirmed(self, fixture: ExternalRef, *, purpose: Purpose) -> bool:  # noqa: ARG002
        """Whatever the test set."""
        return self.lineups


def scripted_provider(world: World, clock: MutableClock) -> ScriptedProvider:
    """Today's and later matches of ``world`` as the provider would list them."""
    upcoming = [f for f in world.fixtures if f.status is FixtureStatus.SCHEDULED]
    fixtures = [_api_fixture(f, number) for number, f in enumerate(upcoming)]
    by_world = {f.ref: api.ref for f, api in zip(upcoming, fixtures, strict=True)}
    odds = [
        replace(snapshot, fixture=by_world[snapshot.fixture])
        for snapshot in world.odds
        if snapshot.fixture in by_world
    ]
    results = {
        by_world[ref]: replace(result, fixture=by_world[ref]) for ref, result in world.feed.items()
    }
    return ScriptedProvider(
        clock=clock,
        fixtures=fixtures,
        odds=odds,
        results=results,
        kickoffs={f.ref: f.kickoff for f in fixtures},
    )


__all__ = ["MANUAL", "START", "ScriptedProvider", "scripted_provider"]
