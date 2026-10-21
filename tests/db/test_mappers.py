"""Domain <-> ORM round trips (app.db.mappers)."""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.history import upsert_batch
from app.db.mappers import (
    fixture_from_row,
    line_to_db,
    market_from_db,
    odds_from_row,
    result_from_row,
)
from app.db.schema import NO_LINE, FixtureRow, OddsSnapshotRow
from app.domain import (
    LINE_STEP,
    DomainValidationError,
    FixtureResult,
    FixtureStatus,
    HistoryBatch,
    Market,
    MarketType,
    ResultStatus,
    Selection,
)
from tests.domain.factories import make_fixture, make_snapshot

LINELESS = [MarketType.MATCH_RESULT, MarketType.DOUBLE_CHANCE, MarketType.BTTS]


@pytest.mark.parametrize("market_type", LINELESS)
def test_lineless_markets_store_no_line(market_type: MarketType) -> None:
    market = Market(market_type)
    assert line_to_db(market) == NO_LINE
    assert market_from_db(market_type, NO_LINE) == market


@given(st.integers(min_value=-40, max_value=40))
def test_asian_handicap_lines_round_trip(steps: int) -> None:
    market = Market.asian_handicap(steps * LINE_STEP)
    assert market_from_db(MarketType.ASIAN_HANDICAP, line_to_db(market)) == market


async def load_fixture(session: AsyncSession) -> FixtureRow:
    query = select(FixtureRow).options(
        selectinload(FixtureRow.league),
        selectinload(FixtureRow.home_team),
        selectinload(FixtureRow.away_team),
    )
    return (await session.execute(query)).scalar_one()


async def test_fixture_result_and_odds_round_trip(db_session: AsyncSession) -> None:
    fixture = make_fixture()
    result = FixtureResult(
        fixture=fixture.ref,
        status=ResultStatus.FINISHED,
        home_score=2,
        away_score=1,
        home_ht_score=1,
        away_ht_score=0,
    )
    snapshot = make_snapshot(fixture, market=Market.totals(2.5), selection=Selection.UNDER)
    await upsert_batch(
        db_session, HistoryBatch(fixtures=(fixture,), results=(result,), odds=(snapshot,))
    )
    await db_session.commit()
    db_session.expunge_all()

    row = await load_fixture(db_session)
    assert fixture_from_row(row) == fixture
    assert result_from_row(row) == result
    odds_row = (await db_session.execute(select(OddsSnapshotRow))).scalar_one()
    assert odds_from_row(odds_row, fixture=fixture.ref) == snapshot


async def test_scheduled_fixture_has_no_result(db_session: AsyncSession) -> None:
    await upsert_batch(
        db_session, HistoryBatch(fixtures=(make_fixture(status=FixtureStatus.SCHEDULED),))
    )
    assert result_from_row(await load_fixture(db_session)) is None


async def test_corrupt_row_raises_instead_of_leaking(db_session: AsyncSession) -> None:
    await upsert_batch(
        db_session, HistoryBatch(fixtures=(make_fixture(status=FixtureStatus.SCHEDULED),))
    )
    # Written behind the repository's back: the mapper must refuse it, not pass it on.
    await db_session.execute(update(FixtureRow).values(status=FixtureStatus.FINISHED))
    with pytest.raises(DomainValidationError, match="needs both scores"):
        result_from_row(await load_fixture(db_session))
