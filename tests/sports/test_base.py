"""Tests for the SportAdapter contract (app.sports.base)."""

from collections.abc import Collection, Sequence
from datetime import date

import pytest

from app.domain import (
    DomainValidationError,
    ExternalRef,
    Fixture,
    FixtureResult,
    HistoryBatch,
    MarketType,
    OddsKind,
    OddsSnapshot,
    ResultStatus,
    Sport,
)
from app.sports.base import FixtureQuery, SportAdapter
from tests.domain.factories import make_fixture, make_snapshot


class InMemoryAdapter:
    """Minimal adapter serving canned data; proves the protocol is implementable."""

    def __init__(self, fixtures: list[Fixture]) -> None:
        self._fixtures = fixtures

    @property
    def sport(self) -> Sport:
        return Sport.FOOTBALL

    def supported_markets(self) -> frozenset[MarketType]:
        return frozenset({MarketType.MATCH_RESULT})

    async def fetch_fixtures(self, query: FixtureQuery) -> list[Fixture]:
        return [
            fixture
            for fixture in self._fixtures
            if query.date_from <= fixture.kickoff.date() <= query.date_to
        ]

    async def fetch_odds(
        self, fixtures: Sequence[ExternalRef], *, kind: OddsKind
    ) -> list[OddsSnapshot]:
        wanted = set(fixtures)
        return [make_snapshot(f, kind=kind) for f in self._fixtures if f.ref in wanted]

    async def fetch_results(self, fixtures: Sequence[ExternalRef]) -> list[FixtureResult]:
        return [
            FixtureResult(fixture=ref, status=ResultStatus.FINISHED, home_score=1, away_score=0)
            for ref in fixtures
        ]

    async def refresh_history(self, leagues: Collection[ExternalRef], season: int) -> HistoryBatch:
        return HistoryBatch(
            fixtures=tuple(
                f for f in self._fixtures if f.league.ref in leagues and f.season == season
            )
        )


async def test_in_memory_adapter_satisfies_the_protocol() -> None:
    fixture = make_fixture()
    adapter: SportAdapter = InMemoryAdapter([fixture])
    day = fixture.kickoff.date()

    assert adapter.sport is Sport.FOOTBALL
    assert MarketType.MATCH_RESULT in adapter.supported_markets()
    assert await adapter.fetch_fixtures(FixtureQuery(date_from=day, date_to=day)) == [fixture]
    odds = await adapter.fetch_odds([fixture.ref], kind=OddsKind.MORNING)
    assert [snapshot.kind for snapshot in odds] == [OddsKind.MORNING]
    assert len(await adapter.fetch_results([fixture.ref])) == 1
    history = await adapter.refresh_history([fixture.league.ref], 2024)
    assert history.fixtures == (fixture,)


def test_fixture_query_rejects_inverted_range() -> None:
    with pytest.raises(DomainValidationError, match="before"):
        FixtureQuery(date_from=date(2024, 8, 2), date_to=date(2024, 8, 1))


def test_fixture_query_defaults_to_whole_scope() -> None:
    query = FixtureQuery(date_from=date(2024, 8, 1), date_to=date(2024, 8, 1))
    assert query.leagues == frozenset()
    assert query.teams == frozenset()
