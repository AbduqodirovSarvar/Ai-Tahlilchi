"""The pipeline over Football-Data.org: fixtures, odds and results reach the database.

Real runtime wiring (one client, call log, usage counter) against respx-mocked answers.
"""

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import func, select

from app.clients.football_data_org import FOOTBALL_DATA_ORG_CONFIG
from app.clients.quota import RequestPurpose
from app.config import Settings
from app.db.engine import create_engine
from app.db.repo import latest_odds, list_fixtures
from app.db.schema import ApiUsageRow, Base, ProviderCallRow
from app.domain import BOOKMAKER_MARKET_AVERAGE, ExternalRef, FixtureStatus, OddsKind, Sport
from app.pipeline.ingest import ingest_fixtures, ingest_odds, ingest_results
from app.pipeline.runtime import open_runtime

DAY = date(2026, 10, 30)
AUTO = RequestPurpose.AUTOMATIC


def body(status: str = "TIMED", **extra: Any) -> dict[str, Any]:
    match = {
        "id": 537800,
        "utcDate": "2026-10-30T16:00:00Z",
        "status": status,
        "area": {"id": 2072, "name": "England"},
        "competition": {"id": 2021, "name": "Premier League", "code": "PL"},
        "season": {"startDate": "2026-08-14"},
        "homeTeam": {"id": 57, "name": "Arsenal FC"},
        "awayTeam": {"id": 61, "name": "Chelsea FC"},
        "odds": {"homeWin": 1.9, "draw": 3.5, "awayWin": 4.1},
    }
    match.update(extra)
    return match


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=FOOTBALL_DATA_ORG_CONFIG.base_url, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
async def settings(tmp_path: Path) -> Settings:
    engine = create_engine(tmp_path / "app.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        db_path=tmp_path / "app.db",
        football_data_org_key=SecretStr("k" * 24),
        football_data_requests_per_minute=600,  # tests must not wait for the pacer
    )


async def test_fixtures_odds_and_results_are_stored(
    settings: Settings, api: respx.MockRouter
) -> None:
    listing = api.get("matches").mock(
        side_effect=[
            respx.MockResponse(json={"matches": [body()]}),  # the day's fixtures
            respx.MockResponse(json={"matches": [body()]}),  # odds by id
            respx.MockResponse(
                json={
                    "matches": [
                        body(
                            "FINISHED",
                            score={
                                "duration": "REGULAR",
                                "fullTime": {"home": 2, "away": 1},
                                "halfTime": {"home": 1, "away": 0},
                            },
                        )
                    ]
                }
            ),
        ]
    )
    async with open_runtime(settings) as runtime:
        ctx = runtime.ctx
        assert await ingest_fixtures(ctx, Sport.FOOTBALL, DAY, DAY, purpose=AUTO) == 1
        start = datetime(2026, 10, 30, tzinfo=UTC)
        async with ctx.sessions() as session:
            page = await list_fixtures(session, start=start, end=start + timedelta(days=1))
        [record] = page.items
        fixture_ref = record.fixture.ref
        assert fixture_ref == ExternalRef("football-data-org", "537800")
        assert record.fixture.league.ref == ExternalRef("football-data", "E0")  # canonical league

        assert (
            await ingest_odds(
                ctx, Sport.FOOTBALL, [fixture_ref], kind=OddsKind.MORNING, purpose=AUTO
            )
            == 3
        )
        async with ctx.sessions() as session:
            stored = await latest_odds(session, [fixture_ref])
        assert {quote.bookmaker for quote in stored[fixture_ref]} == {BOOKMAKER_MARKET_AVERAGE}

        [result] = await ingest_results(ctx, Sport.FOOTBALL, [fixture_ref], purpose=AUTO)
        assert (result.home_score, result.away_score) == (2, 1)
        async with ctx.sessions() as session:
            page = await list_fixtures(session, start=start, end=start + timedelta(days=1))
        assert page.items[0].fixture.status is FixtureStatus.FINISHED

        async with ctx.sessions() as session:
            calls = (
                await session.execute(select(func.count()).select_from(ProviderCallRow))
            ).scalar_one()
            usage = (await session.execute(select(ApiUsageRow))).scalars().all()
        assert calls == 3  # every request is in the call log
        assert [(row.provider, row.requests_used) for row in usage] == [("football-data-org", 3)]
    assert listing.call_count == 3
