"""Async engine and session factory for the application database (SQLite file).

Every connection is configured for a single-process, multi-task workload:

- ``journal_mode=WAL``: readers (bot) never block the writer (scheduler) and vice versa.
- ``busy_timeout``: a writer waits for a competing write lock instead of failing at once.
- ``foreign_keys=ON``: SQLite ignores FOREIGN KEY constraints unless asked per connection.
- ``synchronous=NORMAL``: durable across application crashes in WAL mode, much faster
  than FULL; only an OS crash/power loss can lose the last transactions.

SQLite allows one writer at a time per database file, whatever the number of connections.
Code that must never wait for the application's own long write transactions (API usage
counters) therefore uses its own engine with a short ``busy_timeout``
(:func:`create_usage_engine`) and a store that degrades gracefully when the lock is held.
"""

from functools import partial
from pathlib import Path
from typing import Final

from sqlalchemy import event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine as sqlalchemy_create_async_engine
from sqlalchemy.pool import ConnectionPoolEntry

from app.config import Settings

SQLITE_BUSY_TIMEOUT_MS: Final = 5_000
# Usage counters give up quickly: the caller is about to make a network request.
USAGE_BUSY_TIMEOUT_MS: Final = 250


def sqlite_pragmas(busy_timeout_ms: int) -> tuple[str, ...]:
    """The per-connection PRAGMAs with the given lock wait."""
    return (
        "PRAGMA journal_mode=WAL",
        f"PRAGMA busy_timeout={busy_timeout_ms}",
        "PRAGMA foreign_keys=ON",
        "PRAGMA synchronous=NORMAL",
    )


SQLITE_PRAGMAS: Final = sqlite_pragmas(SQLITE_BUSY_TIMEOUT_MS)


def sqlite_url(db_path: Path) -> str:
    """SQLAlchemy URL of the async SQLite driver for ``db_path``."""
    return f"sqlite+aiosqlite:///{db_path.as_posix()}"


def _apply_sqlite_pragmas(
    dbapi_connection: DBAPIConnection,
    _record: ConnectionPoolEntry,
    pragmas: tuple[str, ...] = SQLITE_PRAGMAS,
) -> None:
    cursor = dbapi_connection.cursor()
    try:
        for pragma in pragmas:
            cursor.execute(pragma)
    finally:
        cursor.close()


def create_engine(
    db_path: Path, *, echo: bool = False, busy_timeout_ms: int = SQLITE_BUSY_TIMEOUT_MS
) -> AsyncEngine:
    """Create an async engine for the SQLite file at ``db_path`` (parent dirs are created).

    The caller owns the engine and must ``await engine.dispose()`` on shutdown.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = sqlalchemy_create_async_engine(sqlite_url(db_path), echo=echo)
    event.listen(
        engine.sync_engine,
        "connect",
        partial(_apply_sqlite_pragmas, pragmas=sqlite_pragmas(busy_timeout_ms)),
    )
    return engine


def create_usage_engine(db_path: Path) -> AsyncEngine:
    """A separate engine for :class:`~app.db.api_usage.SqlUsageStore` (short lock wait).

    Its connections never share a transaction with the application's sessions, and a
    competing writer makes it give up after :data:`USAGE_BUSY_TIMEOUT_MS` instead of the
    full ``busy_timeout`` (the store then keeps the counts in memory until the lock is free).
    """
    return create_engine(db_path, busy_timeout_ms=USAGE_BUSY_TIMEOUT_MS)


def create_engine_from_settings(settings: Settings) -> AsyncEngine:
    """Engine for the database configured by ``DB_PATH``."""
    return create_engine(settings.db_path)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory; objects stay usable after commit (no implicit reload I/O)."""
    return async_sessionmaker(engine, expire_on_commit=False)
