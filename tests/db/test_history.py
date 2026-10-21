"""Idempotent upserts and coverage queries (app.db.history)."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import history
from app.db._sql import dialect_insert, table_of
from app.db.history import (
    SeasonCoverage,
    season_coverage,
    upsert_batch,
)
from app.db.mappers import result_from_row
from app.db.schema import FixtureRow, LeagueRow, OddsSnapshotRow, TeamRow
from app.domain import (
    ExternalRef,
    FixtureResult,
    FixtureStatus,
    HistoryBatch,
    OddsKind,
    ResultStatus,
    Selection,
)
from app.errors import DataError
from tests.domain.factories import KICKOFF, SOURCE, make_fixture, make_snapshot


def finished(fixture_ref: ExternalRef, home: int = 2, away: int = 1) -> FixtureResult:
    return FixtureResult(
        fixture=fixture_ref, status=ResultStatus.FINISHED, home_score=home, away_score=away
    )


def sample_batch() -> HistoryBatch:
    first = make_fixture("A", "B")
    second = make_fixture("B", "C", kickoff=KICKOFF + timedelta(days=7))
    odds = (
        make_snapshot(first, selection=Selection.HOME, odds=2.1),
        make_snapshot(first, selection=Selection.DRAW, odds=3.4),
        make_snapshot(first, selection=Selection.AWAY, odds=3.9),
        make_snapshot(first, selection=Selection.HOME, odds=2.2, kind=OddsKind.OPENING),
    )
    return HistoryBatch(
        fixtures=(first, second),
        results=(finished(first.ref), finished(second.ref, 0, 0)),
        odds=odds,
    )


async def count(session: AsyncSession, model: type[object]) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def table_counts(session: AsyncSession) -> tuple[int, int, int, int]:
    return (
        await count(session, LeagueRow),
        await count(session, TeamRow),
        await count(session, FixtureRow),
        await count(session, OddsSnapshotRow),
    )


async def test_upsert_is_idempotent(db_session: AsyncSession) -> None:
    batch = sample_batch()
    first = await upsert_batch(db_session, batch)
    await db_session.commit()
    second = await upsert_batch(db_session, batch)
    await db_session.commit()

    assert first == second
    assert (first.leagues, first.teams, first.fixtures, first.results, first.odds) == (
        1,
        3,
        2,
        2,
        4,
    )
    assert await table_counts(db_session) == (1, 3, 2, 4)


async def test_reimport_updates_changed_values(db_session: AsyncSession) -> None:
    batch = sample_batch()
    await upsert_batch(db_session, batch)
    corrected_odds = replace(batch.odds[0], odds=2.25)
    corrected_result = finished(batch.fixtures[0].ref, 3, 1)
    await upsert_batch(
        db_session,
        HistoryBatch(fixtures=batch.fixtures, results=(corrected_result,), odds=(corrected_odds,)),
    )
    await db_session.commit()

    prices = (
        await db_session.execute(
            select(OddsSnapshotRow.odds).where(
                OddsSnapshotRow.selection == Selection.HOME,
                OddsSnapshotRow.kind == OddsKind.CLOSING,
            )
        )
    ).scalar_one()
    assert prices == 2.25
    score = (
        await db_session.execute(
            select(FixtureRow.home_score).where(
                FixtureRow.external_id == batch.fixtures[0].ref.external_id
            )
        )
    ).scalar_one()
    assert score == 3


async def test_refresh_without_result_keeps_stored_score(db_session: AsyncSession) -> None:
    batch = sample_batch()
    await upsert_batch(db_session, batch)
    await upsert_batch(db_session, HistoryBatch(fixtures=batch.fixtures))
    scores = (
        await db_session.execute(select(FixtureRow.home_score).order_by(FixtureRow.kickoff_at))
    ).scalars()
    assert list(scores) == [2, 0]


async def test_results_and_odds_for_previously_stored_fixtures(db_session: AsyncSession) -> None:
    fixture = make_fixture(status=FixtureStatus.SCHEDULED)
    await upsert_batch(db_session, HistoryBatch(fixtures=(fixture,)))
    summary = await upsert_batch(
        db_session,
        HistoryBatch(results=(finished(fixture.ref),), odds=(make_snapshot(fixture),)),
    )
    assert (summary.results, summary.odds, summary.orphan_records) == (1, 1, 0)
    row = (await db_session.execute(select(FixtureRow))).scalar_one()
    assert (row.status, row.home_score, row.away_score) == (FixtureStatus.FINISHED, 2, 1)


async def test_records_of_unknown_fixtures_are_skipped(db_session: AsyncSession) -> None:
    ghost = make_fixture("X", "Y")
    summary = await upsert_batch(
        db_session, HistoryBatch(results=(finished(ghost.ref),), odds=(make_snapshot(ghost),))
    )
    assert (summary.results, summary.odds, summary.orphan_records) == (0, 0, 2)
    assert await count(db_session, OddsSnapshotRow) == 0


async def test_duplicates_inside_one_batch_are_merged(db_session: AsyncSession) -> None:
    fixture = make_fixture()
    snapshot = make_snapshot(fixture)
    summary = await upsert_batch(
        db_session,
        HistoryBatch(fixtures=(fixture, fixture), odds=(snapshot, replace(snapshot, odds=2.5))),
    )
    assert (summary.fixtures, summary.odds) == (1, 1)
    stored = (await db_session.execute(select(OddsSnapshotRow.odds))).scalar_one()
    assert stored == 2.5


async def test_empty_batch_writes_nothing(db_session: AsyncSession) -> None:
    summary = await upsert_batch(db_session, HistoryBatch())
    assert summary.fixtures == summary.odds == 0
    assert await table_counts(db_session) == (0, 0, 0, 0)


async def test_season_coverage_counts_pinnacle_closing_1x2(db_session: AsyncSession) -> None:
    await upsert_batch(db_session, sample_batch())
    coverage = await season_coverage(db_session)
    assert coverage == [
        SeasonCoverage(source=SOURCE, league="E0", season=2024, fixtures=2, with_pinnacle_closing=1)
    ]
    assert coverage[0].pinnacle_closing_share == 0.5
    assert await season_coverage(db_session, source="other") == []


def test_empty_coverage_share_is_zero() -> None:
    empty = SeasonCoverage(
        source="s", league="E0", season=2024, fixtures=0, with_pinnacle_closing=0
    )
    assert empty.pinnacle_closing_share == 0.0


def fake_session(dialect: str) -> AsyncSession:
    bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect))
    return cast(AsyncSession, SimpleNamespace(get_bind=lambda: bind))


def test_postgresql_gets_its_own_upsert_statement() -> None:
    insert = dialect_insert(fake_session("postgresql"), table_of(OddsSnapshotRow))
    assert isinstance(insert, postgresql.Insert)


def test_unsupported_dialect_is_refused() -> None:
    with pytest.raises(DataError, match="mysql"):
        dialect_insert(fake_session("mysql"), table_of(OddsSnapshotRow))


async def test_rows_that_cannot_be_read_back_are_reported(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def nothing_found(*_args: object) -> dict[ExternalRef, int]:
        return {}

    monkeypatch.setattr(history, "_ids_by_ref", nothing_found)
    with pytest.raises(DataError, match="could not be read back"):
        await upsert_batch(db_session, sample_batch())


async def stored_fixture(session: AsyncSession) -> FixtureRow:
    session.expunge_all()
    return (await session.execute(select(FixtureRow))).scalar_one()


async def assert_every_row_is_readable(session: AsyncSession) -> None:
    session.expunge_all()
    rows = (await session.execute(select(FixtureRow).options(selectinload("*")))).scalars()
    for row in rows:
        result_from_row(row)  # raises DomainValidationError on an unreadable row


async def test_finished_fixture_without_result_waits_for_its_result(
    db_session: AsyncSession,
) -> None:
    fixture = make_fixture(status=FixtureStatus.FINISHED)
    await upsert_batch(db_session, HistoryBatch(fixtures=(fixture,)))
    row = await stored_fixture(db_session)
    assert (row.status, row.home_score) == (FixtureStatus.SCHEDULED, None)
    await assert_every_row_is_readable(db_session)

    await upsert_batch(db_session, HistoryBatch(results=(finished(fixture.ref),)))
    row = await stored_fixture(db_session)
    assert (row.status, row.home_score, row.away_score) == (FixtureStatus.FINISHED, 2, 1)


async def test_abandoned_then_postponed_clears_the_score(db_session: AsyncSession) -> None:
    fixture = make_fixture(status=FixtureStatus.LIVE)
    abandoned = FixtureResult(
        fixture=fixture.ref, status=ResultStatus.ABANDONED, home_score=1, away_score=0
    )
    await upsert_batch(db_session, HistoryBatch(fixtures=(fixture,), results=(abandoned,)))
    await upsert_batch(
        db_session, HistoryBatch(fixtures=(replace(fixture, status=FixtureStatus.POSTPONED),))
    )
    row = await stored_fixture(db_session)
    assert (row.status, row.home_score, row.away_score) == (FixtureStatus.POSTPONED, None, None)
    await assert_every_row_is_readable(db_session)


@pytest.mark.parametrize("fixture_in_batch", [True, False])
async def test_xg_is_kept_when_a_later_result_has_none(
    db_session: AsyncSession, fixture_in_batch: bool
) -> None:
    fixture = make_fixture()
    with_xg = FixtureResult(
        fixture=fixture.ref,
        status=ResultStatus.FINISHED,
        home_score=2,
        away_score=1,
        home_xg=1.7,
        away_xg=0.9,
    )
    await upsert_batch(db_session, HistoryBatch(fixtures=(fixture,), results=(with_xg,)))
    later = HistoryBatch(
        fixtures=(fixture,) if fixture_in_batch else (), results=(finished(fixture.ref, 3, 1),)
    )
    await upsert_batch(db_session, later)
    row = await stored_fixture(db_session)
    assert (row.home_score, row.home_xg, row.away_xg) == (3, 1.7, 0.9)


async def test_status_changes_never_leave_an_unreadable_row(db_session: AsyncSession) -> None:
    fixture = make_fixture(status=FixtureStatus.SCHEDULED)
    abandoned = FixtureResult(
        fixture=fixture.ref, status=ResultStatus.ABANDONED, home_score=0, away_score=1
    )
    steps = [
        HistoryBatch(fixtures=(fixture,)),
        HistoryBatch(fixtures=(replace(fixture, status=FixtureStatus.FINISHED),)),
        HistoryBatch(results=(abandoned,)),
        HistoryBatch(fixtures=(replace(fixture, status=FixtureStatus.CANCELLED),)),
        HistoryBatch(fixtures=(replace(fixture, status=FixtureStatus.FINISHED),)),
        HistoryBatch(results=(finished(fixture.ref),)),
        HistoryBatch(fixtures=(replace(fixture, status=FixtureStatus.LIVE),)),
    ]
    for step in steps:
        await upsert_batch(db_session, step)
        await assert_every_row_is_readable(db_session)


async def test_corrected_kickoff_moves_closing_prices_instead_of_duplicating(
    db_session: AsyncSession,
) -> None:
    fixture = make_fixture()
    await upsert_batch(
        db_session, HistoryBatch(fixtures=(fixture,), odds=(make_snapshot(fixture),))
    )
    moved = replace(fixture, kickoff=KICKOFF - timedelta(hours=5))
    await upsert_batch(
        db_session, HistoryBatch(fixtures=(moved,), odds=(make_snapshot(moved, odds=2.3),))
    )
    db_session.expunge_all()
    rows = (await db_session.execute(select(OddsSnapshotRow))).scalars().all()
    assert [(row.odds, row.taken_at) for row in rows] == [(2.3, moved.kickoff)]


async def test_time_series_prices_keep_every_moment(db_session: AsyncSession) -> None:
    fixture = make_fixture()
    morning = make_snapshot(fixture, kind=OddsKind.MORNING)
    later = replace(morning, taken_at=morning.taken_at + timedelta(hours=3), odds=2.4)
    summary = await upsert_batch(
        db_session, HistoryBatch(fixtures=(fixture,), odds=(morning, later))
    )
    assert summary.odds == 2
    assert await count(db_session, OddsSnapshotRow) == 2
