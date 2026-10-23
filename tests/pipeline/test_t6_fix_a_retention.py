"""T6 fix round A: pruning old intermediate price snapshots (ADR 0018)."""

from collections import Counter
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.schema import OddsSnapshotRow
from app.domain import BOOKMAKER_PINNACLE, FixtureStatus, HistoryBatch, Market, OddsKind, Selection
from app.pipeline import retention
from app.pipeline.retention import RETENTION_DAYS, run_retention
from tests.integration.t4_support import (
    Bare,
    add_signal,
    finished,
    make_fixture,
    open_bare,
    snapshot,
    x12,
)
from tests.pipeline.world import START

KINDS = (OddsKind.OPENING, OddsKind.MORNING, OddsKind.RECALC, OddsKind.PREMATCH, OddsKind.CLOSING)
MARKET = Market.match_result()
OLD = START - timedelta(days=RETENTION_DAYS + 30)
RECENT = START - timedelta(days=RETENTION_DAYS - 30)


def rows_of(fixture, per_kind: int = 1):  # type: ignore[no-untyped-def]
    rows = []
    for kind in KINDS:
        for index in range(per_kind):
            rows += snapshot(
                fixture,
                BOOKMAKER_PINNACLE,
                MARKET,
                x12(2.0, 3.4, 3.8),
                kind=kind,
                taken_at=fixture.kickoff - timedelta(hours=10 + index),
            )
    return rows


async def kinds_left(bare: Bare, fixture) -> Counter[str]:  # type: ignore[no-untyped-def]
    from tests.integration.t4_support import fixture_id  # noqa: PLC0415

    db_id = await fixture_id(bare.sessions, fixture)
    async with bare.sessions() as session:
        rows = await session.scalars(
            select(OddsSnapshotRow).where(OddsSnapshotRow.fixture_id == db_id)
        )
        return Counter(row.kind.value for row in rows)


async def world(path: Path):  # type: ignore[no-untyped-def]
    old_plain = make_fixture("old-plain", OLD, status=FixtureStatus.FINISHED)
    old_signal = make_fixture("old-signal", OLD, status=FixtureStatus.FINISHED)
    old_open = make_fixture("old-open", OLD, status=FixtureStatus.POSTPONED)
    recent = make_fixture("recent", RECENT, status=FixtureStatus.FINISHED)
    upcoming = make_fixture("upcoming", START + timedelta(days=1))
    fixtures = (old_plain, old_signal, old_open, recent, upcoming)
    odds = tuple(row for fixture in fixtures for row in rows_of(fixture))
    results = tuple(finished(f, 1, 0) for f in (old_plain, old_signal, recent))
    batch = HistoryBatch(fixtures=fixtures, odds=odds, results=results)
    return fixtures, open_bare(path, batch)


async def test_old_intermediate_snapshots_of_finished_matches_nobody_used_are_dropped(
    tmp_path: Path,
) -> None:
    fixtures, opened = await world(tmp_path / "ret.db")
    old_plain, old_signal, old_open, recent, upcoming = fixtures
    async with opened as bare:
        await add_signal(bare, old_signal, MARKET, Selection.HOME, 2.0, Decimal(1))

        report = await run_retention(bare.ctx)

        assert not report.skipped
        assert (
            report.deleted == 9
        )  # morning, recalc, prematch: 3 rows each of the one plain fixture
        assert await kinds_left(bare, old_plain) == Counter(opening=3, closing=3)
        everything = Counter({kind.value: 3 for kind in KINDS})
        assert await kinds_left(bare, old_signal) == everything  # a signal keeps its history
        assert await kinds_left(bare, old_open) == everything  # not finished: still needed
        assert await kinds_left(bare, recent) == everything
        assert await kinds_left(bare, upcoming) == everything


async def test_a_second_run_in_the_same_week_does_nothing_and_a_later_one_finds_nothing(
    tmp_path: Path,
) -> None:
    _, opened = await world(tmp_path / "again.db")
    async with opened as bare:
        first = await run_retention(bare.ctx)
        same_week = await run_retention(bare.ctx)
        bare.clock.now += timedelta(weeks=1)
        next_week = await run_retention(bare.ctx)

        assert (first.deleted, same_week.skipped, next_week.deleted) == (18, True, 0)


async def test_deletes_run_in_batches_until_nothing_is_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retention, "DELETE_BATCH", 4)
    fixtures, opened = await world(tmp_path / "batch.db")
    async with opened as bare:
        report = await run_retention(bare.ctx)

        assert report.deleted == 18  # two old finished fixtures, in batches of 4, 4, 4, 4 and 2
        assert await kinds_left(bare, fixtures[0]) == Counter(opening=3, closing=3)


async def test_a_failure_is_recorded_and_the_next_run_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, opened = await world(tmp_path / "fail.db")
    async with opened as bare:
        real = retention._delete_batch

        async def broken(*_args: object) -> int:
            raise RuntimeError("disk is full")

        monkeypatch.setattr(retention, "_delete_batch", broken)
        with pytest.raises(RuntimeError, match="disk is full"):
            await run_retention(bare.ctx)

        monkeypatch.setattr(retention, "_delete_batch", real)
        bare.clock.now += timedelta(days=1)  # past the backoff of the failed key
        retried = await run_retention(bare.ctx)
        assert retried.deleted == 18


async def test_only_snapshot_rows_are_deleted(
    tmp_path: Path,
) -> None:
    _, opened = await world(tmp_path / "scope.db")
    async with opened as bare:
        async with bare.sessions() as session:
            before = len((await session.scalars(select(OddsSnapshotRow))).all())

        report = await run_retention(bare.ctx)

        async with bare.sessions() as session:
            after = len((await session.scalars(select(OddsSnapshotRow))).all())
        assert before - after == report.deleted
