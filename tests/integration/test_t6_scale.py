"""T6 performance and scale sanity on a production-sized database (marked ``slow``).

Ten leagues x five seasons of results and six months of snapshots; the numbers printed here are
quoted in docs/qa/T6-acceptance.md. The asserted budgets are generous ceilings (a regression
alarm, not a benchmark); the printed ``T6-PERF`` lines are the measurements.
"""

import shutil
import time
import tracemalloc
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import event

from app.db.engine import create_engine, create_session_factory
from app.domain import (
    ExternalRef,
    Fixture,
    FixtureResult,
    FixtureStatus,
    HistoryBatch,
    OddsKind,
    ResultStatus,
    Sport,
)
from app.pipeline.context import PipelineContext
from app.pipeline.freshness import mark_fresh
from app.pipeline.ledger import Ledger
from app.services.interfaces import (
    AnalysisQuery,
    DateSpec,
    ReportRequest,
    UserSettingsView,
)
from app.services.real import RealSystem, create_real_services
from tests.e2e.t6_world import emit
from tests.integration.t6_scale_world import NOW, Scale, build_database
from tests.services.contract_suite import MutableClock

pytestmark = pytest.mark.slow

USER = 4242
ANALYSIS_COLD_CEILING_S = 90.0
ANALYSIS_WARM_CEILING_S = 15.0
REPORT_CEILING_S = 10.0
EXPORT_CEILING_S = 15.0
SIGNALS_FOR_EXPORT = 5_000


@pytest.fixture(scope="module")
def big_template(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Scale]:
    """The big database, built once per module (the build time is printed)."""
    import asyncio  # noqa: PLC0415

    path = tmp_path_factory.mktemp("scale") / "big.db"
    started = time.perf_counter()
    scale = asyncio.run(build_database(path))
    emit(
        f"T6-PERF build: {time.perf_counter() - started:.1f}s fixtures={scale.fixtures} "
        f"finished={scale.finished} odds_rows={scale.odds} upcoming={scale.upcoming} "
        f"db_mb={path.stat().st_size / 1e6:.1f}"
    )
    return path, scale


@dataclass
class Slow:
    """Statements slower than a threshold, collected through the engine's cursor events."""

    threshold: float = 0.25
    seen: list[tuple[float, str]] = field(default_factory=list)

    def attach(self, engine) -> None:  # type: ignore[no-untyped-def]
        sync = engine.sync_engine
        started: dict[int, float] = {}

        @event.listens_for(sync, "before_cursor_execute")
        def _before(conn, cursor, *_rest):  # type: ignore[no-untyped-def]  # noqa: ARG001
            started[id(cursor)] = time.perf_counter()

        @event.listens_for(sync, "after_cursor_execute")
        def _after(conn, cursor, statement, *_rest):  # type: ignore[no-untyped-def]  # noqa: ARG001
            took = time.perf_counter() - started.pop(id(cursor), time.perf_counter())
            if took >= self.threshold:
                self.seen.append((took, " ".join(statement.split())[:140]))

    def report(self, label: str) -> None:
        worst = sorted(self.seen, reverse=True)[:5]
        emit(f"T6-PERF slow-statements[{label}] (>= {self.threshold}s): {len(self.seen)}")
        for took, sql in worst:
            emit(f"T6-PERF   {took:.2f}s  {sql}")


@dataclass
class Big:
    ctx: PipelineContext
    system: RealSystem
    slow: Slow
    clock: MutableClock
    engine: object


@pytest.fixture
async def big(big_template: tuple[Path, Scale], tmp_path: Path) -> AsyncIterator[Big]:
    template, _ = big_template
    target = tmp_path / "big.db"
    shutil.copy(template, target)
    engine = create_engine(target)
    sessions = create_session_factory(engine)
    clock = MutableClock(now=NOW)
    slow = Slow()
    slow.attach(engine)
    ctx = PipelineContext(sessions=sessions, ledger=Ledger(sessions), clock=clock)
    await mark_fresh(ctx)  # analyses read the database only and refuse a stale one
    system = create_real_services(ctx)
    try:
        yield Big(ctx, system, slow, clock, engine)
    finally:
        await engine.dispose()


async def configure(system: RealSystem) -> UserSettingsView:
    current = await system.services.settings.get_settings(USER)
    return await system.services.settings.update_settings(
        USER,
        replace(
            current,
            min_probability=0.30,
            min_ev=0.0,
            max_signals=30,
            quiet_hours=None,
            bank=Decimal(100_000),
            unit=Decimal(100),
        ),
    )


async def test_the_daily_analysis_of_thirty_candidates_on_a_production_sized_database(
    big: Big, big_template: tuple[Path, Scale]
) -> None:
    _, scale = big_template
    await configure(big.system)
    query = AnalysisQuery(
        dates=DateSpec.next_days(5), min_probability=0.30, min_ev=0.0, max_signals=30
    )
    started = time.perf_counter()
    cold = await big.system.services.analysis.analyze(USER, query)
    cold_s = time.perf_counter() - started
    big.clock.advance(timedelta(minutes=11))  # past the 10-minute single-flight/cache windows
    started = time.perf_counter()
    await big.system.services.analysis.analyze(USER, query)
    warm_s = time.perf_counter() - started
    emit(
        f"T6-PERF analysis: candidates={scale.upcoming} cold={cold_s:.2f}s warm={warm_s:.2f}s "
        f"signals={len(cold.signals)}"
    )
    big.slow.report("analysis")
    assert cold.fixtures_analyzed >= scale.upcoming * 0.9
    assert len(cold.signals) > 0
    assert cold_s < ANALYSIS_COLD_CEILING_S
    assert warm_s < ANALYSIS_WARM_CEILING_S


async def test_a_report_and_a_five_thousand_row_export(big: Big) -> None:
    await configure(big.system)
    started = time.perf_counter()
    await seed_settled_signals(big, SIGNALS_FOR_EXPORT)
    emit(f"T6-PERF seed-signals: {SIGNALS_FOR_EXPORT} rows in {time.perf_counter() - started:.1f}s")

    started = time.perf_counter()
    report = await big.system.services.reports.report(USER, ReportRequest.all_time())
    report_s = time.perf_counter() - started
    started = time.perf_counter()
    grouped = await big.system.services.reports.report(
        USER, ReportRequest.all_time(group_by=_first_group())
    )
    grouped_s = time.perf_counter() - started
    started = time.perf_counter()
    export = await big.system.services.reports.export(USER)
    export_s = time.perf_counter() - started
    bets_started = time.perf_counter()
    bets_export = await big.system.services.reports.export_bets(USER)
    bets_s = time.perf_counter() - bets_started
    rows = export.content.count(b"\n")
    emit(
        f"T6-PERF report: all_time={report_s:.2f}s grouped={grouped_s:.2f}s "
        f"export={export_s:.2f}s rows={rows} bytes={len(export.content)} bets_export={bets_s:.2f}s "
        f"bets_bytes={len(bets_export.content)}"
    )
    big.slow.report("report+export")
    assert report.settled >= SIGNALS_FOR_EXPORT * 0.99
    assert grouped.segments
    assert rows >= SIGNALS_FOR_EXPORT
    assert report_s < REPORT_CEILING_S
    assert export_s < EXPORT_CEILING_S


def _first_group():  # type: ignore[no-untyped-def]
    from app.services.interfaces import ReportGroup  # noqa: PLC0415

    return next(iter(ReportGroup))


async def seed_settled_signals(big: Big, count: int) -> None:
    """``count`` settled signals with bets, written straight into the tables (bulk)."""
    import random  # noqa: PLC0415

    from sqlalchemy import select  # noqa: PLC0415

    from app.db.enums import (  # noqa: PLC0415
        Confidence,
        Outcome,
        ResultSource,
        SignalSource,
        SignalStatus,
    )
    from app.db.repo import ensure_user  # noqa: PLC0415
    from app.db.schema import FixtureRow, SignalResultRow, SignalRow, UserBetRow  # noqa: PLC0415
    from app.domain import MarketType, Selection  # noqa: PLC0415

    rng = random.Random(3)  # noqa: S311 - simulation
    async with big.ctx.sessions.begin() as session:
        await ensure_user(session, USER, timezone="Asia/Tashkent")
        ids = [
            (row.id, row.kickoff_at)
            for row in (
                await session.execute(
                    select(FixtureRow).where(FixtureRow.kickoff_at < NOW).limit(count)
                )
            ).scalars()
        ]
        assert len(ids) >= count
        signals = []
        for fixture_id, kickoff in ids[:count]:
            signals.append(
                SignalRow(
                    user_id=USER,
                    fixture_id=fixture_id,
                    market=MarketType.MATCH_RESULT,
                    selection=Selection.HOME,
                    odds=2.0,
                    p_model=0.55,
                    ev=0.1,
                    confidence=Confidence.MEDIUM,
                    stake_units=Decimal("1.0"),
                    source=SignalSource.ANALYSIS,
                    status=SignalStatus.ACTIVE,
                    recommended_at=kickoff - timedelta(days=1),
                    recommended_odds=2.0,
                )
            )
        session.add_all(signals)
        await session.flush()
        hit = [rng.random() < 0.5 for _ in signals]
        session.add_all(
            SignalResultRow(
                signal_id=signal.id,
                outcome=Outcome.WIN if won else Outcome.LOSS,
                source=ResultSource.API,
                profit_units=Decimal("1.0") if won else Decimal("-1.0"),
                clv=rng.gauss(0.01, 0.05),
                settled_at=NOW - timedelta(hours=1),
            )
            for signal, won in zip(signals, hit, strict=True)
        )
        session.add_all(
            UserBetRow(
                user_id=USER,
                signal_id=signal.id,
                odds_taken=2.0,
                stake=Decimal(100),
                placed_at=NOW - timedelta(days=1),
                outcome=Outcome.WIN if won else Outcome.LOSS,
                profit=Decimal(100) if won else Decimal(-100),
                settled_at=NOW - timedelta(hours=1),
            )
            for signal, won in zip(signals[: count // 5], hit, strict=False)
        )


def _day_batch(day: int, now: datetime, *, results_for: list[Fixture]) -> HistoryBatch:
    """One new match per league of the first two leagues (with fresh quotes) plus the results
    of the matches of the previous simulated day."""
    from tests.integration.t6_scale_world import (  # noqa: PLC0415
        _snapshots,
        league_of,
        team_of,
    )

    fixtures: list[Fixture] = []
    odds = []
    for code in ("E0",):
        fixture = Fixture(
            ref=ExternalRef("football-data", f"{code}/sim/{day}"),
            sport=Sport.FOOTBALL,
            league=league_of(code),
            season=2026,
            kickoff=now + timedelta(hours=6),
            home=team_of(code, day % 20),
            away=team_of(code, (day + 7) % 20),
            status=FixtureStatus.SCHEDULED,
        )
        fixtures.append(fixture)
        odds.extend(
            _snapshots(
                fixture,
                code,
                day % 20,
                (day + 7) % 20,
                ((OddsKind.MORNING, now - timedelta(hours=1)),),
            )
        )
    results = tuple(
        FixtureResult(
            fixture=f.ref,
            status=ResultStatus.FINISHED,
            home_score=day % 4,
            away_score=(day + 1) % 3,
        )
        for f in results_for
    )
    return HistoryBatch(fixtures=tuple(fixtures), results=results, odds=tuple(odds))


async def test_memory_stays_flat_over_two_hundred_simulated_days(big: Big) -> None:
    """Loop the daily jobs for 200 days with an injected clock; caches must stay bounded."""
    import gc  # noqa: PLC0415

    from app.db.history import upsert_batch  # noqa: PLC0415
    from app.llm.service import LlmService  # noqa: PLC0415
    from app.pipeline.daily import run_due_digests  # noqa: PLC0415
    from app.pipeline.prematch import run_due_recalcs  # noqa: PLC0415
    from app.pipeline.review import LlmCandidateReviewer  # noqa: PLC0415
    from app.pipeline.settle import run_settlement  # noqa: PLC0415
    from app.pipeline.templates import run_due_templates  # noqa: PLC0415
    from app.services.fake import FakeNotifier  # noqa: PLC0415
    from tests.e2e.t6_world import ScriptedGemini  # noqa: PLC0415

    llm = LlmService(ScriptedGemini())
    ctx = replace(big.ctx, reviewer=LlmCandidateReviewer(llm))
    system = create_real_services(ctx, FakeNotifier())
    await configure(system)
    yesterday: list[Fixture] = []
    marks: dict[int, tuple[int, int]] = {}
    started = time.perf_counter()
    tracemalloc.start(1)
    try:
        for day in range(200):
            big.clock.advance(timedelta(days=1))
            batch = _day_batch(day, big.clock(), results_for=yesterday)
            async with ctx.sessions.begin() as session:
                await upsert_batch(session, batch)
            yesterday = list(batch.fixtures)
            await run_due_digests(ctx, system.delivery)
            await run_due_templates(ctx, system.delivery)
            await run_due_recalcs(ctx, system.delivery)
            await run_settlement(ctx, system.delivery)
            if day in {19, 199}:
                gc.collect()
                marks[day] = (tracemalloc.get_traced_memory()[0], len(ctx.models._cache))
        top = tracemalloc.take_snapshot().statistics("filename")[:3]
    finally:
        tracemalloc.stop()
    growth_mb = (marks[199][0] - marks[19][0]) / 1e6
    emit(
        f"T6-PERF memory: loop={time.perf_counter() - started:.0f}s "
        f"day20={marks[19][0] / 1e6:.1f}MB day200={marks[199][0] / 1e6:.1f}MB "
        f"growth={growth_mb:.1f}MB model_cache={marks[19][1]}->{marks[199][1]} "
        f"llm_journal={len(llm.budget.journal())}"
    )
    for stat in top:
        emit(f"T6-PERF   top allocation {stat.size / 1e6:.1f}MB {stat.traceback}")
    big.slow.report("memory-loop")
    assert growth_mb < 20.0, "in-process memory should plateau (bounded caches)"
    assert marks[199][1] <= 64  # the documented cache capacity
    assert len(llm.budget.journal()) <= 500
