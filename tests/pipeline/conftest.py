"""Fixtures for pipeline unit tests."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.engine import create_engine, create_session_factory
from app.db.schema import Base
from app.pipeline.ledger import Ledger
from tests.pipeline.harness import Env, open_env
from tests.pipeline.providers import scripted_provider
from tests.pipeline.world import World


@pytest.fixture
async def sessions(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(tmp_path / "unit.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await Ledger.ensure_schema(engine)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.fixture
def ledger(sessions: async_sessionmaker[AsyncSession]) -> Ledger:
    return Ledger(sessions)


@pytest.fixture
async def provider_env(history_template: "tuple[Path, World]", tmp_path: Path) -> AsyncIterator[Env]:
    """History only plus a scripted provider; results are revealed by the provider."""
    template, world = history_template
    async with open_env(
        template,
        world,
        tmp_path / "provider.db",
        provider_factory=lambda clock: scripted_provider(world, clock),
        reveal_results=False,
    ) as opened:
        yield opened


@pytest.fixture
async def seeded_env(seeded_template: "tuple[Path, World]", tmp_path: Path) -> AsyncIterator[Env]:
    """The full seeded world, no provider; results appear when the clock passes them."""
    template, world = seeded_template
    async with open_env(template, world, tmp_path / "seeded.db") as opened:
        yield opened
