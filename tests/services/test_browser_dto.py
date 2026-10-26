"""Input validation of the browser requests and the paging arithmetic, over many page shapes."""

import asyncio
from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain import FixtureStatus, MarketType, OddsKind, Selection, Sport
from app.services.errors import ValidationFailedError
from app.services.fake import create_fake_services
from app.services.interfaces import (
    MAX_PAGE_SIZE,
    StoredFixture,
    StoredFixtureRequest,
    StoredOdds,
    StoredOddsRequest,
    StoredTeamRequest,
)
from tests.services.contract_suite import MutableClock

LONG_ID = "x" * 41


@pytest.mark.parametrize(
    "build",
    [
        lambda: StoredFixtureRequest(page=-1),
        lambda: StoredFixtureRequest(page_size=0),
        lambda: StoredFixtureRequest(page_size=MAX_PAGE_SIZE + 1),
        lambda: StoredFixtureRequest(date_from=date(2026, 10, 2), date_to=date(2026, 10, 1)),
        lambda: StoredFixtureRequest(league_ids=frozenset({LONG_ID})),
        lambda: StoredFixtureRequest(team_ids=frozenset({" "})),
        lambda: StoredTeamRequest(text="x" * 65),
        lambda: StoredTeamRequest(page_size=0),
        lambda: StoredOddsRequest(fixture_id=LONG_ID),
        lambda: StoredOddsRequest(fixture_id="1", page=-3),
    ],
)
def test_a_bad_request_is_a_validation_error(build: object) -> None:
    with pytest.raises(ValidationFailedError):
        build()  # type: ignore[operator]


def test_a_single_day_range_and_the_largest_page_are_valid() -> None:
    day = date(2026, 10, 1)

    request = StoredFixtureRequest(date_from=day, date_to=day, page_size=MAX_PAGE_SIZE)

    assert request.page_size == MAX_PAGE_SIZE


def test_instants_must_be_utc() -> None:
    local = datetime(2026, 10, 21, 11, 0, tzinfo=timezone(timedelta(hours=5)))
    kwargs = {
        "fixture_id": "1",
        "sport": Sport.FOOTBALL,
        "league_id": "2",
        "league": "L",
        "country": None,
        "home_team": "A",
        "away_team": "B",
        "home_team_id": "3",
        "away_team_id": "4",
        "status": FixtureStatus.SCHEDULED,
        "home_score": None,
        "away_score": None,
        "has_odds": False,
        "source": "s",
    }
    with pytest.raises(ValidationFailedError):
        StoredFixture(kickoff=local, **kwargs)  # type: ignore[arg-type]
    assert StoredFixture(kickoff=local.astimezone(UTC), **kwargs).label == "A — B"  # type: ignore[arg-type]
    with pytest.raises(ValidationFailedError):
        StoredOdds(
            bookmaker="b",
            market=MarketType.MATCH_RESULT,
            selection=Selection.HOME,
            line=0.0,
            odds=2.0,
            kind=OddsKind.MORNING,
            taken_at=datetime(2026, 10, 21),  # noqa: DTZ001 - the naive instant is the point
        )


@given(page=st.integers(min_value=0, max_value=40), size=st.integers(1, MAX_PAGE_SIZE))
def test_paging_covers_every_fixture_exactly_once(page: int, size: int) -> None:
    browser = create_fake_services(clock=MutableClock()).browser

    async def run() -> tuple[int, int, int, int]:
        everything = await browser.list_stored_fixtures(
            1, StoredFixtureRequest(page_size=MAX_PAGE_SIZE)
        )
        total = everything.total
        shown = await browser.list_stored_fixtures(
            1, StoredFixtureRequest(page=page, page_size=size)
        )
        return total, shown.total, shown.page_count, len(shown.items)

    total, shown_total, page_count, shown_items = asyncio.run(run())

    assert shown_total == total
    assert page_count == -(-total // size)
    assert shown_items == max(0, min(size, total - page * size))
