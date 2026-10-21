"""Database fixtures: a fresh SQLite file per test with the full schema."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.engine import create_engine, create_session_factory
from app.db.schema import Base


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "db" / "test.db"


@pytest.fixture
async def engine(db_path: Path) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(db_path)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with create_session_factory(engine)() as session:
        yield session
