"""FootballDataProvider: usage counting, translated errors and the runtime wiring."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.clients.football_data_org import usage_counter
from app.clients.football_data_org_models import Match
from app.clients.football_data_org_requests import MatchFilters
from app.clients.quota import InMemoryUsageStore, RequestPurpose
from app.config import Settings
from app.db.engine import create_engine
from app.db.schema import Base
from app.domain import BOOKMAKER_MARKET_AVERAGE, ExternalRef, FixtureResult, ResultStatus, Sport
from app.errors import ExternalServiceError
from app.pipeline.errors import ProviderUnavailableError
from app.pipeline.provider import FootballDataProvider
from app.pipeline.runtime import open_runtime
from app.sports.base import FixtureQuery

NOW = datetime(2026, 10, 29, 8, 0, tzinfo=UTC)


class DownClient:
    async def matches(self, _filters: MatchFilters, **_: RequestPurpose) -> list[Match]:
        raise ExternalServiceError("football-data-org: HTTP 503")


async def test_quota_reports_counted_requests_against_the_daily_ceiling() -> None:
    store = InMemoryUsageStore()
    guard = usage_counter(store, 10)
    await guard.charge(RequestPurpose.AUTOMATIC)
    provider = FootballDataProvider(DownClient(), quota=guard)  # type: ignore[arg-type]
    snapshot = await provider.quota()
    assert (snapshot.provider, snapshot.used, snapshot.limit) == ("football-data-org", 1, 14_400)
    assert snapshot.available(RequestPurpose.AUTOMATIC) == 14_399
    assert provider.source == "football-data-org"
    assert provider.sport is Sport.FOOTBALL


async def test_provider_failures_become_pipeline_errors() -> None:
    provider = FootballDataProvider(DownClient(), quota=usage_counter(InMemoryUsageStore(), 10))  # type: ignore[arg-type]
    day = NOW.date()
    with pytest.raises(ProviderUnavailableError) as info:
        await provider.adapter(RequestPurpose.AUTOMATIC).fetch_fixtures(
            FixtureQuery(date_from=day, date_to=day)
        )
    assert info.value.provider == "football-data-org"


async def test_results_pass_through_without_expected_goals() -> None:
    provider = FootballDataProvider(DownClient(), quota=usage_counter(InMemoryUsageStore(), 10))  # type: ignore[arg-type]
    result = FixtureResult(
        fixture=ExternalRef("football-data-org", "1"),
        status=ResultStatus.FINISHED,
        home_score=1,
        away_score=0,
    )
    assert await provider.enrich_results(
        [result], purpose=RequestPurpose.AUTOMATIC, max_requests=5
    ) == [result]


async def test_runtime_wires_one_client_for_feed_and_provider(tmp_path: Path) -> None:
    engine = create_engine(tmp_path / "wired.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        db_path=tmp_path / "wired.db",
        football_data_org_key=SecretStr("k" * 24),
        football_data_requests_per_minute=30,
    )
    async with open_runtime(settings) as runtime:
        assert runtime.matches is not None
        provider = runtime.ctx.providers[Sport.FOOTBALL]
        assert isinstance(provider, FootballDataProvider)
        assert (await provider.quota()).limit == 30 * 24 * 60
        assert runtime.ctx.config.bet_bookmaker == BOOKMAKER_MARKET_AVERAGE
