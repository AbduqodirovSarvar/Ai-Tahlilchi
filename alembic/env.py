"""Alembic environment: async SQLite, database path from the application settings.

Path resolution (first match wins): ``-x db_path=...`` on the command line, the
``db_path`` attribute set by code that calls Alembic programmatically (tests), then
``DB_PATH`` from the settings.

Migrations run with SQLite foreign keys OFF: batch migrations rebuild tables by copy +
DROP, and with enforcement ON a DROP of a parent table would cascade-delete child rows.
Integrity is verified with ``PRAGMA foreign_key_check`` after the migration instead.
"""

import asyncio
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy import Enum, event, pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import ConnectionPoolEntry

from app.config import get_settings
from app.db.engine import sqlite_url
from app.db.schema import Base
from app.db.types import ENUM_LENGTH, MONEY_PRECISION, MONEY_SCALE, Money, UTCDateTime
from app.errors import DataError

config = context.config
target_metadata = Base.metadata

# Programmatic callers (tests, the app) keep their own logging configuration.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def database_path() -> Path:
    """The SQLite file to migrate."""
    from_cli = context.get_x_argument(as_dictionary=True).get("db_path")
    if from_cli:
        return Path(from_cli)
    from_caller = config.attributes.get("db_path")
    if from_caller is not None:
        return Path(from_caller)
    return get_settings().db_path


def render_item(type_: str, obj: object, _context: AutogenContext) -> str | bool:
    """Render application column types as plain SQLAlchemy types in migration files.

    Migrations are frozen history: they must not import (and silently change with) app
    code. The Python-side conversions of these types live in ``app.db.types``.
    """
    if type_ != "type":
        return False
    if isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    if isinstance(obj, Money):
        return (
            f"sa.Numeric(precision={MONEY_PRECISION}, scale={MONEY_SCALE})"
            f".with_variant(sa.String(length={MONEY_PRECISION + 2}), 'sqlite')"
        )
    if isinstance(obj, Enum) and not obj.native_enum:
        return f"sa.String(length={ENUM_LENGTH})"
    return False


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it (``alembic upgrade head --sql``)."""
    context.configure(
        url=sqlite_url(database_path()),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def _disable_foreign_keys(dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=OFF")
    finally:
        cursor.close()


def do_run_migrations(connection: Connection) -> None:
    """Run the migrations, then refuse to finish if they left dangling foreign keys."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()
        violations = connection.execute(text("PRAGMA foreign_key_check")).fetchall()
        if violations:
            msg = f"migration left {len(violations)} foreign key violation(s): {violations[:5]}"
            raise DataError(msg)


async def run_async_migrations() -> None:
    """Open a dedicated connection (no pooling) and migrate through it."""
    db_path = database_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(sqlite_url(db_path), poolclass=pool.NullPool)
    event.listen(engine.sync_engine, "connect", _disable_foreign_keys)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
