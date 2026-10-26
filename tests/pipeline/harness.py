"""A wired real-services environment over a seeded temporary SQLite database."""

import shutil
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.engine import create_engine, create_session_factory
from app.db.history import upsert_batch
from app.db.repo import upsert_live
from app.db.schema import Base
from app.domain import Sport
from app.pipeline.config import PipelineConfig
from app.pipeline.context import PipelineContext
from app.pipeline.delivery import NullDelivery
from app.pipeline.freshness import mark_fresh
from app.pipeline.ledger import Ledger
from app.pipeline.provider import SportProvider
from app.pipeline.review import CandidateReviewer, NoReview
from app.pipeline.settle import run_settlement
from app.services.fake import FakeNotifier
from app.services.real import RealSystem, create_real_services
from tests.pipeline.world import START, World, build_world
from tests.services.contract_suite import MutableClock


def build_template(path: Path, *, history_only: bool = False) -> World:
    """Create the seeded database file once per test session."""
    import asyncio  # noqa: PLC0415

    world = build_world()

    async def seed() -> None:
        engine = create_engine(path)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        await Ledger.ensure_schema(engine)
        async with create_session_factory(engine).begin() as session:
            await upsert_batch(session, world.history_batch() if history_only else world.batch())
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
async def open_env(  # noqa: PLR0913 - one switch per thing a test may vary
    template: Path,
    world: World,
    target: Path,
    *,
    provider_factory: Callable[[MutableClock], SportProvider] | None = None,
    config: PipelineConfig | None = None,
    reveal_results: bool = True,
    reviewer: CandidateReviewer | None = None,
    fresh: bool = True,
) -> AsyncIterator[Env]:
    """A fresh copy of the seeded database wired into the real services.

    ``fresh``: the database counts as refreshed at the start (analyses read the database only
    and refuse a stale one); pass ``False`` to test the stale case.
    """
    shutil.copy(template, target)
    engine = create_engine(target)
    sessions = create_session_factory(engine)
    clock = MutableClock(now=START)
    providers = {} if provider_factory is None else {Sport.FOOTBALL: provider_factory(clock)}
    ctx = PipelineContext(
        sessions=sessions,
        ledger=Ledger(sessions),
        providers=providers,
        clock=clock,
        config=config or PipelineConfig(),
        reviewer=reviewer or NoReview(),
    )
    if fresh:
        await mark_fresh(ctx)
    notifier = FakeNotifier()
    system = create_real_services(ctx, notifier)
    feed = ResultFeed(ctx, world, NullDelivery())
    if reveal_results:
        clock.after_advance = feed
    try:
        yield Env(ctx, system, notifier, clock, world, engine, sessions, feed)
    finally:
        await engine.dispose()
