"""SQLite-backed aiogram FSM storage: wizard state survives a restart (ADR 0006 section 5).

It lives in its own small database file, not in the application database: the FSM tables are
not part of the Alembic-managed schema (``alembic check`` stays clean), a busy wizard never
competes with the pipeline for the application's single writer lock, and losing the file
only resets in-progress wizards, so it is deliberately left out of the backups.

Failure modes: FSM data is JSON; a row that cannot be decoded (manual edit, older format)
is treated as empty with a warning instead of breaking the user's next message. Opening is
lazy, so building the dispatcher never touches the disk.
"""

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import aiosqlite
from aiogram.exceptions import DataNotDictLikeError
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, DefaultKeyBuilder, StateType, StorageKey

from app.logging import get_logger

logger = get_logger(__name__)

FSM_DB_NAME: Final = "fsm.db"
_SCHEMA: Final = (
    "CREATE TABLE IF NOT EXISTS fsm ("
    " key TEXT PRIMARY KEY,"
    " state TEXT,"
    " data TEXT NOT NULL DEFAULT '{}')"
)
_BUSY_TIMEOUT_MS: Final = 5_000


def fsm_db_path(db_path: Path) -> Path:
    """Where the FSM database lives: next to the application database."""
    return db_path.with_name(FSM_DB_NAME)


class SqliteStorage(BaseStorage):
    """Persistent FSM storage; one row per ``(bot, chat, user, destiny)``."""

    def __init__(self, path: Path) -> None:
        """Store FSM records in the SQLite file ``path`` (created on first use)."""
        self._path = path
        self._keys = DefaultKeyBuilder(with_bot_id=True, with_destiny=True)
        self._connection: aiosqlite.Connection | None = None
        self._open_lock = asyncio.Lock()

    async def _db(self) -> aiosqlite.Connection:
        """The shared connection, opened (and the table created) on first use."""
        async with self._open_lock:
            if self._connection is None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                connection = await aiosqlite.connect(self._path)
                await connection.execute("PRAGMA journal_mode=WAL")
                await connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
                await connection.execute(_SCHEMA)
                await connection.commit()
                self._connection = connection
            return self._connection

    async def close(self) -> None:
        """Close the connection (the dispatcher does this on shutdown)."""
        async with self._open_lock:
            if self._connection is not None:
                await self._connection.close()
                self._connection = None

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        """Store the state name (``None`` clears it)."""
        name = state.state if isinstance(state, State) else state
        db = await self._db()
        await db.execute(
            "INSERT INTO fsm (key, state) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET state = excluded.state",
            (self._keys.build(key), name),
        )
        await self._commit(db, key)

    async def get_state(self, key: StorageKey) -> str | None:
        """The stored state name, if any."""
        row = await self._row(key)
        return None if row is None else row[0]

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        """Replace the data; it must be a JSON-serialisable dict."""
        if not isinstance(data, dict):
            msg = f"Data must be a dict or dict-like object, got {type(data).__name__}"
            raise DataNotDictLikeError(msg)
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        db = await self._db()
        await db.execute(
            "INSERT INTO fsm (key, data) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET data = excluded.data",
            (self._keys.build(key), payload),
        )
        await self._commit(db, key)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        """The stored data; empty when absent or undecodable."""
        row = await self._row(key)
        if row is None:
            return {}
        try:
            decoded = json.loads(row[1])
        except json.JSONDecodeError:
            logger.warning("fsm_data_undecodable", key=self._keys.build(key))
            return {}
        if not isinstance(decoded, dict):
            logger.warning("fsm_data_not_a_dict", key=self._keys.build(key))
            return {}
        return decoded

    async def _row(self, key: StorageKey) -> tuple[str | None, str] | None:
        db = await self._db()
        async with db.execute(
            "SELECT state, data FROM fsm WHERE key = ?", (self._keys.build(key),)
        ) as cursor:
            row = await cursor.fetchone()
        return None if row is None else (row[0], row[1])

    async def _commit(self, db: aiosqlite.Connection, key: StorageKey) -> None:
        """Commit, dropping a record with neither state nor data (finished chats leave no row)."""
        await db.execute(
            "DELETE FROM fsm WHERE key = ? AND state IS NULL AND data = '{}'",
            (self._keys.build(key),),
        )
        await db.commit()
