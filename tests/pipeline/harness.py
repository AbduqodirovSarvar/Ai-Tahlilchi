"""A wired real-services environment over a seeded temporary SQLite database."""

import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.engine import create_engine, create_session_factory
from app.db.history import upsert_batch
from app.db.repo import upsert_live
from app.db.schema import Base
from app.pipeline.config import PipelineConfig
from app.pipeline.context import PipelineContext
from app.pipeline.delivery import NullDelivery
from app.pipeline.ledger import Ledger
from app.pipeline.provider import SportProvider
from app.pipeline.settle import run_settlement
from app.services.fake import FakeNotifier
from app.services.real import RealSystem, create_real_services
from tests.pipeline.world import START, World, build_world
from tests.services.contract_suite import MutableClock


def build_template(path: Path) -> World:
    """Create the seeded database file once per test session."""
    import asyncio  # noqa: PLC0415

    world = build_world()

    async def seed() -> None:
        engine = create_engine(path)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await Ledger.ensure_schema(engine)
        async with create_session_factory(engine).begin() as session:
            await upsert_batch(session, world.batch())
        await engine.dispose()

    asyncio.run(seed())
    return world


@dataclass
class ResultFeed:
    """Reveals the prepared results of matches whose time has passed, then settles."""

    ctx: PipelineContext
    world: World
    delivery: NullDelivery

    async def __call__(self, now: datetime) -> None:
        """Finish every match that ended before ``now`` and settle the signals on it."""
        kickoffs = {f.ref: f.kickoff for f in self.world.fixtures}
        over = self.ctx.config.match_duration
        due = [r for ref, r in self.world.feed.items() if kickoffs[ref] + over <= now]
        async with self.ctx.sessions.begin() as session:
            await upsert_live(session, results=due)
        await run_settlement(self.ctx, self.delivery)


@dataclass
class Env:
    """Context, services, the recording notifier and the controllable clock."""

    ctx: PipelineContext
    system: RealSystem
    notifier: FakeNotifier
    clock: MutableClock
    world: World
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    feed: ResultFeed


@asynccontextmanager
async def open_env(
    template: Path,
    world: World,
    target: Path,
    *,
    providers: dict | None = None,  # type: ignore[type-arg]
    config: PipelineConfig | None = None,
) -> AsyncIterator[Env]:
    """A fresh copy of the seeded database wired into the real services."""
    shutil.copy(template, target)
    engine = create_engine(target)
    sessions = create_session_factory(engine)
    clock = MutableClock(now=START)
    ctx = PipelineContext(
        sessions=sessions,
        ledger=Ledger(sessions),
        providers=providers or {},
        clock=clock,
        config=config or PipelineConfig(),
    )
    notifier = FakeNotifier()
    system = create_real_services(ctx, notifier)
    feed = ResultFeed(ctx, world, NullDelivery())
    clock.after_advance = feed
    try:
        yield Env(ctx, system, notifier, clock, world, engine, sessions, feed)
    finally:
        await engine.dispose()


type Providers = dict[object, SportProvider]
