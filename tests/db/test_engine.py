"""Tests for app.db.engine."""

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import ConnectionPoolEntry

from app.config import Settings
from app.db.engine import (
    SQLITE_BUSY_TIMEOUT_MS,
    _apply_sqlite_pragmas,
    create_engine,
    create_engine_from_settings,
    create_session_factory,
    sqlite_url,
)


def test_sqlite_url_uses_async_driver_and_forward_slashes() -> None:
    assert sqlite_url(Path("data") / "app.db") == "sqlite+aiosqlite:///data/app.db"


async def test_every_connection_gets_the_pragmas(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        journal_mode = (await connection.execute(text("PRAGMA journal_mode"))).scalar_one()
        foreign_keys = (await connection.execute(text("PRAGMA foreign_keys"))).scalar_one()
        busy_timeout = (await connection.execute(text("PRAGMA busy_timeout"))).scalar_one()
        synchronous = (await connection.execute(text("PRAGMA synchronous"))).scalar_one()
    assert journal_mode == "wal"
    assert foreign_keys == 1
    assert busy_timeout == SQLITE_BUSY_TIMEOUT_MS
    assert synchronous == 1  # NORMAL


async def test_engine_creates_missing_parent_directories(tmp_path: Path) -> None:
    db_path = tmp_path / "a" / "b" / "app.db"
    engine = create_engine(db_path)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    finally:
        await engine.dispose()
    assert db_path.is_file()


async def test_engine_from_settings_uses_db_path(tmp_path: Path) -> None:
    engine = create_engine_from_settings(Settings(db_path=tmp_path / "x.db"))
    try:
        assert engine.url.database is not None
        assert engine.url.database.endswith("x.db")
    finally:
        await engine.dispose()


async def test_session_factory_keeps_objects_loaded_after_commit(engine: AsyncEngine) -> None:
    factory = create_session_factory(engine)
    assert factory.kw["expire_on_commit"] is False


def test_pragma_hook_configures_a_raw_connection(tmp_path: Path) -> None:
    # Called directly: under the async driver the hook runs inside a greenlet that the
    # coverage tracer does not follow.
    with closing(sqlite3.connect(tmp_path / "raw.db")) as connection:
        _apply_sqlite_pragmas(connection, cast(ConnectionPoolEntry, None))
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
