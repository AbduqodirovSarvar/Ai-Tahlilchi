"""Small SQL helpers shared by the storage modules (private to ``app.db``)."""

from sqlalchemy import Table
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.schema import Base
from app.errors import DataError


def table_of(model: type[Base]) -> Table:
    """The table an ORM model maps."""
    table = model.__table__
    if not isinstance(table, Table):  # pragma: no cover - every model here maps one table
        msg = f"{model.__name__} is not mapped to a single table"
        raise TypeError(msg)
    return table


def dialect_insert(session: AsyncSession, table: Table) -> sqlite.Insert | postgresql.Insert:
    """``INSERT`` supporting ``ON CONFLICT`` for the session's backend (SQLite/PostgreSQL)."""
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        return sqlite.insert(table)
    if dialect == "postgresql":
        return postgresql.insert(table)
    msg = f"upsert is not implemented for the {dialect!r} dialect"
    raise DataError(msg)
