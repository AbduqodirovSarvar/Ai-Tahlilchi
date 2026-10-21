"""Column types that keep storage portable between SQLite and PostgreSQL.

- :class:`UTCDateTime`: timezone-aware UTC in Python on every backend (SQLite has no
  timezone type and would otherwise hand back naive datetimes).
- :class:`Money`: exact :class:`~decimal.Decimal` on every backend (SQLite has no decimal
  type; going through REAL would introduce binary rounding).
- :func:`enum_column`: enums stored as their string values, without a database CHECK, so a
  new sport or market is a code change only (validated in Python on write).
"""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Final, override

from sqlalchemy import DateTime, Dialect, Enum, Numeric, String
from sqlalchemy.types import TypeDecorator, TypeEngine

from app.errors import DataError

MONEY_PRECISION: Final = 18
MONEY_SCALE: Final = 4
MONEY_QUANTUM: Final = Decimal(1).scaleb(-MONEY_SCALE)
_MONEY_TEXT_LENGTH: Final = MONEY_PRECISION + 2  # sign and decimal point
ENUM_LENGTH: Final = 32


class UTCDateTime(TypeDecorator[datetime]):
    """Timestamp stored in UTC; naive datetimes are refused instead of guessed."""

    impl = DateTime(timezone=True)
    cache_ok = True

    @override
    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            msg = f"refusing to store naive datetime {value.isoformat()}; use UTC-aware values"
            raise DataError(msg)
        utc = value.astimezone(UTC)
        # SQLite stores text without an offset; strip it so values sort and compare as UTC.
        return utc.replace(tzinfo=None) if dialect.name == "sqlite" else utc

    @override
    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Money(TypeDecorator[Decimal]):
    """Exact decimal amount with ``MONEY_SCALE`` decimal places.

    PostgreSQL uses ``NUMERIC(18, 4)``; SQLite stores the canonical decimal text, which
    round-trips exactly (aggregate money in Python, not with SQL ``SUM`` on SQLite).
    """

    impl = Numeric(MONEY_PRECISION, MONEY_SCALE, asdecimal=True)
    cache_ok = True

    @override
    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(_MONEY_TEXT_LENGTH))
        return dialect.type_descriptor(self.impl)

    @override
    def process_bind_param(self, value: object, dialect: Dialect) -> Decimal | str | None:
        if value is None:
            return None
        if not isinstance(value, Decimal) or not value.is_finite():
            msg = f"money must be a finite Decimal (got {value!r})"
            raise DataError(msg)
        quantized = value.quantize(MONEY_QUANTUM)
        return str(quantized) if dialect.name == "sqlite" else quantized

    @override
    def process_result_value(self, value: object, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value)).quantize(MONEY_QUANTUM)
        except InvalidOperation as exc:
            msg = f"stored money value is not a decimal: {value!r}"
            raise DataError(msg) from exc


def enum_column[E: StrEnum](enum_type: type[E]) -> Enum:
    """String column holding ``enum_type`` values (not names), with no database CHECK."""
    return Enum(
        enum_type,
        native_enum=False,
        create_constraint=False,
        length=ENUM_LENGTH,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )
