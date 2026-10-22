"""Builders shared by the T4 QA tests: a bare database, hand-made fixtures and prices.

Nothing here calls the code under test to *compute* an expected value; it only wires the
system up (database, context, services) and seeds rows.
"""

from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.engine import create_engine, create_session_factory
from app.db.enums import Confidence, SignalSource, SignalVersionReason
from app.db.history import upsert_batch
from app.db.repo import (
    SignalDraft,
    SignalRecord,
    Thresholds,
    ensure_user,
    list_fixtures,
    upsert_signal,
)
from app.db.schema import Base
from app.domain import (
    BOOKMAKER_1XBET,
    BOOKMAKER_PINNACLE,
    ExternalRef,
    Fixture,
    FixtureResult,
    FixtureStatus,
    HistoryBatch,
    Market,
    OddsKind,
    OddsSnapshot,
    ResultStatus,
    Selection,
    Sport,
)
from app.pipeline.config import PipelineConfig
from app.pipeline.context import PipelineContext
from app.pipeline.ledger import Ledger
from app.pipeline.provider import SportProvider
from app.pipeline.review import CandidateReviewer, NoReview
from app.services.fake import FakeNotifier
from app.services.interfaces import (
    AnalysisQuery,
    AnalysisResult,
    DateSpec,
    DeliveryKey,
    MessageRef,
    Services,
)
from app.services.real import RealSystem, create_real_services
from tests.pipeline.world import START, league_of, team_of
from tests.services.contract_suite import MutableClock

SOURCE = "football-data"
USER = 7001
OTHER_USER = 7002


@dataclass
class Bare:
    """A wired real system over a database that holds only what a test seeds."""

    ctx: PipelineContext
    system: RealSystem
    notifier: FakeNotifier
    clock: MutableClock
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]


@asynccontextmanager
async def open_bare(  # noqa: PLR0913 - one switch per thing a test may vary
    path: Path,
    batch: HistoryBatch | None = None,
    *,
    providers: Callable[[MutableClock], dict[Sport, SportProvider]] | None = None,
    config: PipelineConfig | None = None,
    reviewer: CandidateReviewer | None = None,
    notifier: FakeNotifier | None = None,
    now: datetime = START,
) -> AsyncIterator[Bare]:
    """A fresh database with ``batch`` stored, wired into the real services."""
    engine = create_engine(path)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await Ledger.ensure_schema(engine)
    sessions = create_session_factory(engine)
    if batch is not None:
        async with sessions.begin() as session:
            await upsert_batch(session, batch)
    clock = MutableClock(now=now)
    ctx = PipelineContext(
        sessions=sessions,
        ledger=Ledger(sessions),
        providers={} if providers is None else providers(clock),
        clock=clock,
        config=config or PipelineConfig(),
        reviewer=reviewer or NoReview(),
    )
    notifier = notifier or FakeNotifier()
    system = create_real_services(ctx, notifier)
    try:
        yield Bare(ctx, system, notifier, clock, engine, sessions)
    finally:
        await engine.dispose()


def make_fixture(
    name: str,
    kickoff: datetime,
    *,
    status: FixtureStatus = FixtureStatus.SCHEDULED,
    league: str = "E0",
) -> Fixture:
    """A scheduled football-data fixture ``name`` (home ``name-H``, away ``name-A``)."""
    return Fixture(
        ref=ExternalRef(SOURCE, f"{league}/2026/{name}-H/{name}-A"),
        sport=Sport.FOOTBALL,
        league=league_of(league),
        season=2026,
        kickoff=kickoff,
        home=team_of(f"{name}-H"),
        away=team_of(f"{name}-A"),
        status=status,
    )


def finished(
    fixture: Fixture, home: int, away: int, status: ResultStatus = ResultStatus.FINISHED
) -> FixtureResult:
    """A result with the regular-time score."""
    return FixtureResult(fixture=fixture.ref, status=status, home_score=home, away_score=away)


def snapshot(  # noqa: PLR0913 - the columns of one price row
    fixture: Fixture,
    bookmaker: str,
    market: Market,
    prices: dict[Selection, float],
    *,
    kind: OddsKind,
    taken_at: datetime,
) -> list[OddsSnapshot]:
    """One complete quote as snapshot rows."""
    return [
        OddsSnapshot(
            fixture=fixture.ref,
            bookmaker=bookmaker,
            market=market,
            selection=selection,
            odds=odds,
            taken_at=taken_at,
            kind=kind,
        )
        for selection, odds in prices.items()
    ]


def x12(home: float, draw: float, away: float) -> dict[Selection, float]:
    """1X2 prices."""
    return {Selection.HOME: home, Selection.DRAW: draw, Selection.AWAY: away}


def both(pinnacle: Iterable[OddsSnapshot], one_x_bet: Iterable[OddsSnapshot]) -> list[OddsSnapshot]:
    """Concatenate two bookmakers' rows."""
    return [*pinnacle, *one_x_bet]


async def fixture_id(sessions: async_sessionmaker[AsyncSession], fixture: Fixture) -> int:
    """Database id of a stored fixture."""
    async with sessions() as session:
        page = await list_fixtures(
            session,
            start=fixture.kickoff - timedelta(days=1),
            end=fixture.kickoff + timedelta(days=1),
        )
    return next(record.id for record in page.items if record.fixture.ref == fixture.ref)


async def add_signal(  # noqa: PLR0913, PLR0917 - the columns of one signal
    bare: Bare,
    fixture: Fixture,
    market: Market,
    selection: Selection,
    odds: float,
    stake_units: Decimal,
    *,
    user_id: int = USER,
    ev: float = 0.1,
) -> SignalRecord:
    """Store a recommended signal directly (a hand-made pick with exact numbers)."""
    db_id = await fixture_id(bare.sessions, fixture)
    async with bare.sessions.begin() as session:
        await ensure_user(session, user_id, timezone="Asia/Tashkent")
        return await upsert_signal(
            session,
            SignalDraft(
                user_id=user_id,
                fixture_id=db_id,
                market=market,
                selection=selection,
                odds=odds,
                p_model=0.5,
                ev=ev,
                confidence=Confidence.MEDIUM,
                stake_units=stake_units,
                source=SignalSource.ANALYSIS,
                thresholds=Thresholds(0.3, 0.0, 1.01, 100.0),
                recommended_at=bare.clock(),
            ),
            reason=SignalVersionReason.INITIAL,
        )


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """A UTC instant."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


class Crash(BaseException):
    """Raised after a message went out to model a kill between sending and bookkeeping."""


class FlakyNotifier(FakeNotifier):
    """A recording notifier that can fail ``fail[method]`` times or crash after sending."""

    def __init__(self) -> None:
        super().__init__()
        self.fail: dict[str, int] = {}
        self.crash_after: set[str] = set()

    def _before(self, method: str) -> None:
        if self.fail.get(method, 0) > 0:
            self.fail[method] -= 1
            msg = f"telegram is down ({method})"
            raise ConnectionError(msg)

    def _after(self, method: str) -> None:
        if method in self.crash_after:
            self.crash_after.discard(method)
            raise Crash(method)

    async def send_daily_digest(
        self, user_id: int, digest: Any, *, key: DeliveryKey
    ) -> MessageRef | None:
        self._before("daily_digest")
        sent = await super().send_daily_digest(user_id, digest, key=key)
        self._after("daily_digest")
        return sent

    async def send_signal_update(
        self, user_id: int, update: Any, *, key: DeliveryKey, reply_to: MessageRef | None = None
    ) -> MessageRef | None:
        self._before("signal_update")
        sent = await super().send_signal_update(user_id, update, key=key, reply_to=reply_to)
        self._after("signal_update")
        return sent

    async def send_result_settled(
        self, user_id: int, notice: Any, *, key: DeliveryKey
    ) -> MessageRef | None:
        self._before("result_settled")
        sent = await super().send_result_settled(user_id, notice, key=key)
        self._after("result_settled")
        return sent

    def count(self, method: str) -> int:
        """How many messages of this kind were delivered."""
        return sum(1 for item in self.sent if item.method == method)


async def permissive(services: Services, user_id: int, **changes: Any) -> None:
    """Loose thresholds, no quiet hours: whatever has a positive EV is recommended."""
    settings = await services.settings.get_settings(user_id)
    values = {"min_probability": 0.30, "min_ev": 0.0, "quiet_hours": None, **changes}
    await services.settings.update_settings(user_id, replace(settings, **values))


async def analyse_as(
    services: Services, user_id: int, dates: DateSpec, **changes: Any
) -> AnalysisResult:
    """The user's own settings as a query for ``dates`` (``changes`` replace query fields)."""
    settings = await services.settings.get_settings(user_id)
    query = replace(AnalysisQuery.from_settings(settings, dates), **changes)
    return await services.analysis.analyze(user_id, query)


__all__ = [
    "BOOKMAKER_1XBET",
    "BOOKMAKER_PINNACLE",
    "OTHER_USER",
    "SOURCE",
    "START",
    "USER",
    "Bare",
    "Crash",
    "FlakyNotifier",
    "add_signal",
    "analyse_as",
    "both",
    "finished",
    "fixture_id",
    "make_fixture",
    "open_bare",
    "permissive",
    "snapshot",
    "utc",
    "x12",
]
