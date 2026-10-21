"""Tests for app.db.types."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import Numeric, String, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.schema import UserRow
from app.db.types import Money, UTCDateTime
from app.errors import DataError

PG = postgresql.dialect()
SQLITE = sqlite.dialect()


async def test_datetimes_round_trip_as_utc(db_session: AsyncSession) -> None:
    tashkent = timezone(timedelta(hours=5))
    created = datetime(2024, 8, 16, 23, 30, tzinfo=tashkent)
    db_session.add(UserRow(id=1, timezone="Asia/Tashkent", created_at=created))
    await db_session.commit()
    db_session.expunge_all()

    loaded = (await db_session.execute(select(UserRow.created_at))).scalar_one()
    assert loaded == created
    assert loaded.tzinfo is UTC


async def test_naive_datetimes_are_refused(db_session: AsyncSession) -> None:
    db_session.add(UserRow(id=1, timezone="UTC", created_at=datetime(2024, 1, 1)))  # noqa: DTZ001
    with pytest.raises(StatementError) as caught:
        await db_session.commit()
    assert isinstance(caught.value.orig, DataError)


def test_utc_datetime_keeps_offset_for_postgresql() -> None:
    utc_type = UTCDateTime()
    aware = datetime(2024, 1, 1, 12, tzinfo=timezone(timedelta(hours=2)))
    bound = utc_type.process_bind_param(aware, PG)
    assert bound == aware
    assert bound is not None
    assert bound.tzinfo is UTC
    assert utc_type.process_result_value(aware, PG) == aware
    assert utc_type.process_bind_param(None, PG) is None
    assert utc_type.process_result_value(None, PG) is None


@given(
    st.decimals(
        min_value=Decimal("-99999999999999"),
        max_value=Decimal("99999999999999"),
        places=4,
        allow_nan=False,
        allow_infinity=False,
    )
)
def test_money_round_trips_exactly_through_sqlite_text(amount: Decimal) -> None:
    money = Money()
    stored = money.process_bind_param(amount, SQLITE)
    assert isinstance(stored, str)
    assert money.process_result_value(stored, SQLITE) == amount


async def test_money_round_trips_through_the_database(db_session: AsyncSession) -> None:
    db_session.add(UserRow(id=1, timezone="UTC", bank=Decimal("0.1") + Decimal("0.2")))
    await db_session.commit()
    db_session.expunge_all()
    bank = (await db_session.execute(select(UserRow.bank))).scalar_one()
    assert bank == Decimal("0.3000")


def test_money_quantizes_and_uses_numeric_on_postgresql() -> None:
    money = Money()
    assert money.process_bind_param(Decimal("1.23456"), PG) == Decimal("1.2346")
    assert isinstance(money.load_dialect_impl(PG), Numeric)
    assert isinstance(money.load_dialect_impl(SQLITE), String)
    assert money.process_bind_param(None, PG) is None
    assert money.process_result_value(None, PG) is None


@pytest.mark.parametrize("value", [1.5, Decimal("NaN"), Decimal("Infinity"), "1.0"])
def test_money_rejects_floats_and_non_finite_values(value: object) -> None:
    with pytest.raises(DataError, match="finite Decimal"):
        Money().process_bind_param(value, SQLITE)


def test_money_rejects_corrupt_stored_value() -> None:
    with pytest.raises(DataError, match="not a decimal"):
        Money().process_result_value("abc", SQLITE)
