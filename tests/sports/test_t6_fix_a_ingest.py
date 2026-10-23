"""T6 fix round A: only the bookmakers the pipeline reads are fetched, cached and stored."""

import copy
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import httpx
import pytest
import respx

from app.clients.api_football_models import OddsItem
from app.domain import (
    BOOKMAKER_1XBET,
    BOOKMAKER_PINNACLE,
    ExternalRef,
    OddsKind,
)
from app.pipeline.config import PipelineConfig
from app.sports.football import FootballAdapter
from app.sports.football_mapping import map_odds
from tests.clients.conftest import BASE_URL, TEST_KEY, FakeTime, Harness, build_harness, load

TAKEN = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
NAMES = ("Pinnacle", "1xBet", "Bet365", "Bwin", "William Hill", "Unibet")


def odds_body() -> dict[str, object]:
    body = load("odds_fixture_1208100.json")
    assert isinstance(body, dict)
    record = body["response"][0]  # type: ignore[index]
    template = record["bookmakers"][0]
    record["bookmakers"] = [
        {**copy.deepcopy(template), "id": index, "name": name} for index, name in enumerate(NAMES)
    ]
    return body


def item() -> OddsItem:
    return OddsItem.model_validate(odds_body()["response"][0])  # type: ignore[index]


def test_map_odds_without_a_selection_keeps_every_bookmaker() -> None:
    snapshots, _ = map_odds([item()], kind=OddsKind.MORNING, taken_at=TAKEN)

    assert {s.bookmaker for s in snapshots} == {
        BOOKMAKER_PINNACLE,
        BOOKMAKER_1XBET,
        "bet365",
        "bwin",
        "william-hill",
        "unibet",
    }


def test_map_odds_keeps_only_the_selected_bookmakers() -> None:
    everything, _ = map_odds([item()], kind=OddsKind.MORNING, taken_at=TAKEN)
    kept, _ = map_odds(
        [item()],
        kind=OddsKind.MORNING,
        taken_at=TAKEN,
        bookmakers={BOOKMAKER_PINNACLE, BOOKMAKER_1XBET},
    )

    assert {s.bookmaker for s in kept} == {BOOKMAKER_PINNACLE, BOOKMAKER_1XBET}
    assert len(kept) * 3 == len(everything)  # the same rows for those two, nothing else
    none, _ = map_odds([item()], kind=OddsKind.MORNING, taken_at=TAKEN, bookmakers=set())
    assert none == []


def test_the_pipeline_ingests_pinnacle_the_users_bookmaker_and_the_configured_extras() -> None:
    assert PipelineConfig().ingest_bookmakers == {BOOKMAKER_PINNACLE, BOOKMAKER_1XBET}
    custom = PipelineConfig(bet_bookmaker="bet365", extra_bookmakers=frozenset({"unibet"}))
    assert custom.ingest_bookmakers == {BOOKMAKER_PINNACLE, "bet365", "unibet"}


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
async def harness(router: respx.MockRouter) -> AsyncIterator[Harness]:  # noqa: ARG001
    async with httpx.AsyncClient(
        base_url=BASE_URL, headers={"x-apisports-key": TEST_KEY}
    ) as client:
        yield build_harness(FakeTime(), client)


async def test_the_adapter_stores_and_caches_only_the_wanted_bookmakers(
    harness: Harness, router: respx.MockRouter
) -> None:
    route = router.get("/odds").respond(json=odds_body())
    narrow = FootballAdapter(
        harness.football,
        clock=harness.time.utc,
        bookmakers={BOOKMAKER_PINNACLE, BOOKMAKER_1XBET},
    )
    ref = ExternalRef("api-football", "1208100")

    snapshots = await narrow.fetch_odds([ref], kind=OddsKind.MORNING)
    cached = harness.cache.estimated_bytes
    again = await narrow.fetch_odds([ref], kind=OddsKind.MORNING)

    assert {s.bookmaker for s in snapshots} == {BOOKMAKER_PINNACLE, BOOKMAKER_1XBET}
    assert {s.bookmaker for s in again} == {BOOKMAKER_PINNACLE, BOOKMAKER_1XBET}
    assert route.call_count == 1  # the second call came from the cache

    harness.cache.clear()
    wide = FootballAdapter(harness.football, clock=harness.time.utc)
    await wide.fetch_odds([ref], kind=OddsKind.MORNING)
    assert cached < harness.cache.estimated_bytes  # the reduced body is the smaller one
