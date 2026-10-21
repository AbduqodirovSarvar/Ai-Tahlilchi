"""Async engine and session factory for the application database (SQLite file).

Every connection is configured for a single-process, multi-task workload:

- ``journal_mode=WAL``: readers (bot) never block the writer (scheduler) and vice versa.
- ``busy_timeout``: a writer waits for a competing write lock instead of failing at once.
- ``foreign_keys=ON``: SQLite ignores FOREIGN KEY constraints unless asked per connection.
- ``synchronous=NORMAL``: durable across application crashes in WAL mode, much faster
  than FULL; only an OS crash/power loss can lose the last transactions.
"""

from pathlib import Path
from typing import Final

from sqlalchemy import event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine as sqlalchemy_create_async_engine
from sqlalchemy.pool import ConnectionPoolEntry

from app.config import Settings

SQLITE_BUSY_TIMEOUT_MS: Final = 5_000
SQLITE_PRAGMAS: Final = (
    "PRAGMA journal_mode=WAL",
    f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}",
    "PRAGMA foreign_keys=ON",
    "PRAGMA synchronous=NORMAL",
)


def sqlite_url(db_path: Path) -> str:
    """SQLAlchemy URL of the async SQLite driver for ``db_path``."""
    return f"sqlite+aiosqlite:///{db_path.as_posix()}"


def _apply_sqlite_pragmas(dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
    cursor = dbapi_connection.cursor()
    try:
        for pragma in SQLITE_PRAGMAS:
            cursor.execute(pragma)
    finally:
        cursor.close()


def create_engine(db_path: Path, *, echo: bool = False) -> AsyncEngine:
    """Create an async engine for the SQLite file at ``db_path`` (parent dirs are created).

    The caller owns the engine and must ``await engine.dispose()`` on shutdown.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = sqlalchemy_create_async_engine(sqlite_url(db_path), echo=echo)
    event.listen(engine.sync_engine, "connect", _apply_sqlite_pragmas)
    return engine


def create_engine_from_settings(settings: Settings) -> AsyncEngine:
    """Engine for the database configured by ``DB_PATH``."""
    return create_engine(settings.db_path)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory; objects stay usable after commit (no implicit reload I/O)."""
    return async_sessionmaker(engine, expire_on_commit=False)
