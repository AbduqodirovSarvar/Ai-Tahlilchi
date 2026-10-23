"""Repository reads and writes of the learning loop."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repo import (
    CalibrationRecord,
    SegmentRecord,
    active_calibration,
    add_calibration_version,
    add_learning_change,
    get_learning_change,
    list_calibration_versions,
    list_learning_changes,
    list_segment_rows,
    mark_change_rolled_back,
    save_segment_row,
    set_active_calibration,
    upsert_signal,
)
from app.db.schema import LeagueRow
from app.domain import MarketType, Sport
from tests.db.test_repo_signals import draft, setup
from tests.domain.factories import make_league

NOW = datetime(2026, 11, 2, tzinfo=UTC)


def calibration(number: int, *, gate: bool = True, active: bool = False) -> CalibrationRecord:
    return CalibrationRecord(
        version=number,
        method="isotonic",
        params={"x": [0.5], "y": [0.5]},
        n_samples=300,
        brier_before=0.25,
        brier_after=0.24,
        active=active,
        passed_gate=gate,
        created_at=NOW,
    )


async def test_raw_probability_round_trips_and_defaults_to_none(db_session: AsyncSession) -> None:
    fixture_id, _ = await setup(db_session)
    plain = await upsert_signal(db_session, draft(fixture_id))
    assert plain.p_raw is None

    calibrated = await upsert_signal(db_session, draft(fixture_id, p_model=0.5, p_raw=0.58))
    assert (calibrated.p_model, calibrated.p_raw) == (0.5, 0.58)


async def test_only_one_calibration_is_active_and_none_is_possible(
    db_session: AsyncSession,
) -> None:
    await add_calibration_version(db_session, calibration(1, active=True))
    await add_calibration_version(db_session, calibration(2))
    await add_calibration_version(db_session, calibration(3, gate=False))
    active = await active_calibration(db_session)
    assert active is not None
    assert active.version == 1

    await set_active_calibration(db_session, 2)
    active = await active_calibration(db_session)
    assert active is not None
    assert active.version == 2
    assert [v.active for v in await list_calibration_versions(db_session)] == [
        False,
        True,
        False,
    ]

    await set_active_calibration(db_session, None)
    assert await active_calibration(db_session) is None
    assert [v.passed_gate for v in await list_calibration_versions(db_session)] == [
        True,
        True,
        False,
    ]


async def test_a_version_that_never_passed_its_gate_is_never_the_active_one(
    db_session: AsyncSession,
) -> None:
    await add_calibration_version(db_session, calibration(1, gate=False))
    await set_active_calibration(db_session, 1)  # flagged active, but never earned it
    assert await active_calibration(db_session) is None


async def test_segment_rows_upsert_by_their_key(db_session: AsyncSession) -> None:
    await setup(db_session)  # creates the league
    league = make_league()
    league_id = await db_session.scalar(
        select(LeagueRow.id).where(LeagueRow.external_id == league.ref.external_id)
    )
    assert league_id is not None
    record = SegmentRecord(
        sport=Sport.FOOTBALL,
        league_id=league_id,
        league_ref=league.ref,
        market=MarketType.TOTALS,
        odds_bucket="1.80-2.20",
        n=60,
        roi=-0.05,
        clv=None,
        calib_bias=0.02,
        ev_threshold=0.0,
        enabled=True,
        state_since=None,
        hold_until=None,
    )
    await save_segment_row(db_session, record, now=NOW)
    await save_segment_row(
        db_session, replace(record, n=80, enabled=False, state_since=NOW), now=NOW
    )
    rows = await list_segment_rows(db_session)
    assert len(rows) == 1
    assert (rows[0].n, rows[0].enabled, rows[0].state_since) == (80, False, NOW)
    assert rows[0].league_ref == league.ref


async def test_the_change_journal_lists_newest_first_and_marks_once(
    db_session: AsyncSession,
) -> None:
    first = await add_learning_change(
        db_session, kind="a", subject="s", before={}, after={}, reason="r", now=NOW
    )
    second = await add_learning_change(
        db_session,
        kind="b",
        subject="s",
        before={"x": 1},
        after={"x": 2},
        reason="r",
        now=NOW + timedelta(days=1),
    )

    assert [c.id for c in await list_learning_changes(db_session)] == [second.id, first.id]
    recent = await list_learning_changes(db_session, since=NOW + timedelta(hours=1))
    assert [c.id for c in recent] == [second.id]
    assert (await get_learning_change(db_session, second.id)) == second
    assert await get_learning_change(db_session, 999) is None
    assert await mark_change_rolled_back(db_session, first.id)
    assert not await mark_change_rolled_back(db_session, first.id)
    assert not await mark_change_rolled_back(db_session, 999)
