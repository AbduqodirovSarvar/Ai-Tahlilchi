"""The pipeline's provider layer over respx-mocked API-Football (recorded-shape fixtures)."""

from collections.abc import AsyncIterator, Iterator
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from app.config import Settings
from app.domain import ExternalRef, OddsKind, Sport
from app.pipeline.clock import system_clock
from app.pipeline.errors import BudgetExhaustedError, ProviderUnavailableError
from app.pipeline.provider import AUTOMATIC, MANUAL, FootballProvider, GuardedAdapter
from app.pipeline.runtime import open_runtime
from app.sports.base import FixtureQuery
from tests.clients.conftest import BASE_URL, TEST_KEY, FakeTime, build_harness, load

FIXTURE = ExternalRef("api-football", "1208100")


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
async def provider(router: respx.MockRouter) -> AsyncIterator[FootballProvider]:  # noqa: ARG001
    async with httpx.AsyncClient(base_url=BASE_URL, headers={"x-apisports-key": TEST_KEY}) as http:
        harness = build_harness(FakeTime(), http, daily_limit=10)
        yield FootballProvider(harness.football, clock=harness.time.utc)


async def test_metadata_and_quota(provider: FootballProvider) -> None:
    assert provider.sport is Sport.FOOTBALL
    assert provider.source == "api-football"
    assert provider.markets()
    snapshot = await provider.quota()
    assert (snapshot.used, snapshot.limit) == (0, 10)
    assert snapshot.available(MANUAL) == 10
    assert snapshot.available(AUTOMATIC) == snapshot.automatic_available == 7


async def test_adapter_calls_charge_the_right_purpose(
    provider: FootballProvider, router: respx.MockRouter
) -> None:
    router.get("/fixtures").respond(json=load("fixtures_date_2024-10-26.json"))
    router.get("/odds").respond(json=load("odds_fixture_1208100.json"))

    fixtures = await provider.adapter(MANUAL).fetch_fixtures(
        FixtureQuery(date_from=date(2024, 10, 26), date_to=date(2024, 10, 26))
    )
    odds = await provider.adapter(AUTOMATIC).fetch_odds([FIXTURE], kind=OddsKind.MORNING)

    assert fixtures
    assert odds
    snapshot = await provider.quota()
    assert (snapshot.used, snapshot.manual_used) == (2, 1)


async def test_lineups_and_expected_goals(
    provider: FootballProvider, router: respx.MockRouter
) -> None:
    router.get("/fixtures/lineups").respond(json=load("lineups_fixture_1208100.json"))
    router.get("/fixtures").respond(json=load("fixtures_ids.json"))
    assert await provider.lineups_confirmed(FIXTURE, purpose=AUTOMATIC)
    router.get("/fixtures/lineups").respond(json=load("lineups_empty.json"))
    assert not await provider.lineups_confirmed(ExternalRef("api-football", "1"), purpose=AUTOMATIC)
    results = await provider.adapter(AUTOMATIC).fetch_results([ExternalRef("api-football", "1208021")])
    router.get("/fixtures/statistics").respond(json=load("statistics_fixture_1208021.json"))
    enriched = await provider.enrich_results(results, purpose=AUTOMATIC)
    assert len(enriched) == len(results)


async def test_a_spent_quota_becomes_a_budget_error(router: respx.MockRouter) -> None:
    router.get("/fixtures").respond(json=load("fixtures_date_2024-10-26.json"))
    async with httpx.AsyncClient(base_url=BASE_URL, headers={"x-apisports-key": TEST_KEY}) as http:
        harness = build_harness(FakeTime(), http, daily_limit=3)
        await harness.quota.charge(AUTOMATIC, 2)  # leaves only the manual reserve
        provider = FootballProvider(harness.football, clock=harness.time.utc)
        query = FixtureQuery(date_from=date(2024, 10, 26), date_to=date(2024, 10, 26))
        with pytest.raises(BudgetExhaustedError):
            await provider.adapter(AUTOMATIC).fetch_fixtures(query)  # reserve kept for the user
        assert await provider.adapter(MANUAL).fetch_fixtures(query)


async def test_provider_failures_become_unavailable_errors(router: respx.MockRouter) -> None:
    router.get("/fixtures").respond(json=load("error_token.json"))
    router.get("/odds").respond(json=load("error_token.json"))
    async with httpx.AsyncClient(base_url=BASE_URL, headers={"x-apisports-key": TEST_KEY}) as http:
        harness = build_harness(FakeTime(), http)
        adapter = GuardedAdapter(
            FootballProvider(harness.football, clock=harness.time.utc).adapter(MANUAL), "api-football"
        )
        assert adapter.sport is Sport.FOOTBALL
        assert adapter.supported_markets()
        with pytest.raises(ProviderUnavailableError):
            await adapter.fetch_fixtures(
                FixtureQuery(date_from=date(2024, 10, 26), date_to=date(2024, 10, 26))
            )
        with pytest.raises(ProviderUnavailableError):
            await adapter.fetch_results([FIXTURE])
        with pytest.raises(ProviderUnavailableError):
            await adapter.fetch_odds([FIXTURE], kind=OddsKind.MORNING)
        with pytest.raises(ProviderUnavailableError):
            await adapter.refresh_history([ExternalRef("api-football", "39")], 2024)


async def test_runtime_without_a_key_is_database_only(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, db_path=tmp_path / "rt.db")  # type: ignore[call-arg]
    async with open_runtime(settings, clock=system_clock) as runtime:
        assert runtime.ctx.providers == {}
        assert runtime.http is None


async def test_runtime_with_a_key_wires_the_football_provider(tmp_path: Path) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, db_path=tmp_path / "rt.db", api_football_key="x" * 20
    )
    async with open_runtime(settings) as runtime:
        assert set(runtime.ctx.providers) == {Sport.FOOTBALL}
        assert runtime.http is not None
    assert runtime.http.is_closed
