"""The feedback-loop repository: post-mortems, lessons, team records, notes, settled bets."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.db.enums import LessonStatus, Outcome, PostmortemCategory
from app.db.repo import add_signal_note, create_bet, ensure_user, settle_bet
from app.db.repo.feedback import (
    LessonRecord,
    add_postmortem,
    list_lessons,
    list_postmortems,
    lost_signal_ids,
    pick_postmortem,
    save_lesson,
    team_records,
    user_notes_for_teams,
)
from app.db.repo.operations import bets_settled_between
from tests.pipeline.feedback_world import (
    NOW,
    OTHER,
    SETTLED,
    USER,
    finished_fixture,
    open_world,
    place,
    settle,
)

LONG_AGO = NOW - timedelta(days=500)


async def test_the_fixture_record_carries_the_catalog_ids(tmp_path: Path) -> None:
    fixture = finished_fixture("IDS")
    async with open_world(tmp_path / "ids.db", (fixture, 1, 0)) as bare:
        record = (await place(bare, fixture)).fixture

    assert record.league_id and record.home_team_id and record.away_team_id
    assert record.home_team_id != record.away_team_id  # leagues and teams count separately


async def test_adding_a_post_mortem_twice_keeps_the_first(tmp_path: Path) -> None:
    fixture = finished_fixture("DUP")
    async with open_world(tmp_path / "dup.db", (fixture, 0, 1)) as bare:
        signal = await place(bare, fixture)
        async with bare.sessions.begin() as session:
            first = await add_postmortem(
                session,
                signal_id=signal.id,
                category=PostmortemCategory.MODEL_ERROR,
                reason="Birinchi",
                tags=["team:1"],
                created_at=NOW,
            )
            second = await add_postmortem(
                session,
                signal_id=signal.id,
                category=PostmortemCategory.BAD_LUCK,
                reason="Ikkinchi",
                tags=["team:2"],
                created_at=NOW + timedelta(hours=1),
            )
        async with bare.sessions() as session:
            rows = await list_postmortems(session, since=LONG_AGO)

    assert second == first
    assert len(rows) == 1
    assert rows[0].reason == "Birinchi"


async def test_a_post_mortem_for_a_missing_signal_is_an_error(tmp_path: Path) -> None:
    async with open_world(tmp_path / "missing.db") as bare, bare.sessions.begin() as session:
        with pytest.raises(LookupError):
            await add_postmortem(
                session,
                signal_id=999,
                category=PostmortemCategory.BAD_LUCK,
                reason="",
                tags=[],
                created_at=NOW,
            )


async def test_list_postmortems_is_bounded_and_ordered(tmp_path: Path) -> None:
    fixtures = [finished_fixture(f"ORD{n}") for n in range(3)]
    async with open_world(tmp_path / "order.db", *((f, 0, 1) for f in fixtures)) as bare:
        signals = [await place(bare, f) for f in fixtures]
        async with bare.sessions.begin() as session:
            for index, signal in enumerate(signals):
                await add_postmortem(
                    session,
                    signal_id=signal.id,
                    category=PostmortemCategory.MISSING_INFO,
                    reason=f"r{index}",
                    tags=[],
                    created_at=NOW - timedelta(days=index),
                )
        async with bare.sessions() as session:
            everything = await list_postmortems(session, since=LONG_AGO)
            recent = await list_postmortems(session, since=NOW - timedelta(days=1))

    assert [row.reason for row in everything] == ["r2", "r1", "r0"]  # oldest first
    assert [row.reason for row in recent] == ["r1", "r0"]  # the bound itself is included


async def test_pick_postmortem_finds_another_users_explanation_of_the_same_pick(
    tmp_path: Path,
) -> None:
    fixture, other_fixture = finished_fixture("PK"), finished_fixture("PK2")
    async with open_world(tmp_path / "pick.db", (fixture, 0, 1), (other_fixture, 0, 1)) as bare:
        mine = await place(bare, fixture)
        theirs = await place(bare, fixture, user_id=OTHER)
        elsewhere = await place(bare, other_fixture)
        async with bare.sessions.begin() as session:
            await add_postmortem(
                session,
                signal_id=mine.id,
                category=PostmortemCategory.MISSING_INFO,
                reason="Sabab",
                tags=[],
                created_at=NOW,
            )
        async with bare.sessions() as session:
            twin = await pick_postmortem(session, theirs.id)
            unrelated = await pick_postmortem(session, elsewhere.id)

    assert twin is not None
    assert twin.signal_id == mine.id
    assert twin.fixture_id == mine.fixture.id
    assert unrelated is None


async def test_lost_signal_ids_respects_the_bound_the_order_and_the_limit(tmp_path: Path) -> None:
    fixtures = [finished_fixture(f"L{n}") for n in range(3)]
    async with open_world(tmp_path / "lost.db", *((f, 0, 1) for f in fixtures)) as bare:
        old, mid, new = [await place(bare, f) for f in fixtures]
        await settle(bare, old, at=SETTLED - timedelta(days=5))
        await settle(bare, mid, at=SETTLED - timedelta(days=2))
        await settle(bare, new, at=SETTLED)
        async with bare.sessions() as session:
            since_mid = await lost_signal_ids(
                session, settled_since=SETTLED - timedelta(days=2), limit=10
            )
            limited = await lost_signal_ids(session, settled_since=LONG_AGO, limit=2)

    assert since_mid == [new.id, mid.id]  # newest first; the boundary is included
    assert limited == [new.id, mid.id]


async def test_lessons_are_ordered_strongest_first_and_filtered_by_status(tmp_path: Path) -> None:
    async with open_world(tmp_path / "less.db") as bare:
        async with bare.sessions.begin() as session:
            for confirmations, status in (
                (2, LessonStatus.ACTIVE),
                (5, LessonStatus.ACTIVE),
                (1, LessonStatus.PENDING),
            ):
                await save_lesson(
                    session,
                    LessonRecord(
                        id=None,
                        text=f"c{confirmations}",
                        tags=(f"t{confirmations}",),
                        confirmations=confirmations,
                        status=status,
                        first_seen=NOW,
                        last_seen=NOW,
                    ),
                )
        async with bare.sessions() as session:
            everything = await list_lessons(session)
            active = await list_lessons(session, statuses=(LessonStatus.ACTIVE,))

    assert [r.text for r in everything] == ["c5", "c2", "c1"]
    assert [r.text for r in active] == ["c5", "c2"]


async def test_saving_a_lesson_with_its_id_updates_it_and_a_missing_id_fails(
    tmp_path: Path,
) -> None:
    async with open_world(tmp_path / "upd.db") as bare:
        async with bare.sessions.begin() as session:
            created = await save_lesson(
                session,
                LessonRecord(
                    id=None,
                    text="eski",
                    tags=("a",),
                    confirmations=1,
                    status=LessonStatus.PENDING,
                    first_seen=NOW,
                    last_seen=NOW,
                ),
            )
        async with bare.sessions.begin() as session:
            updated = await save_lesson(
                session,
                LessonRecord(
                    id=created.id,
                    text="yangi",
                    tags=("a",),
                    confirmations=2,
                    status=LessonStatus.ACTIVE,
                    first_seen=NOW,
                    last_seen=NOW + timedelta(days=1),
                ),
            )
        async with bare.sessions() as session:
            [stored] = await list_lessons(session)
        async with bare.sessions.begin() as session:
            with pytest.raises(LookupError):
                await save_lesson(session, replace(stored, id=999))

    assert updated.id == created.id
    assert (stored.text, stored.confirmations, stored.status) == ("yangi", 2, LessonStatus.ACTIVE)


async def test_team_records_without_teams_is_empty(tmp_path: Path) -> None:
    async with open_world(tmp_path / "empty.db") as bare, bare.sessions() as session:
        assert await team_records(session, [], since=LONG_AGO) == {}
        assert await user_notes_for_teams(session, USER, [], since=LONG_AGO, limit=3) == []


async def test_notes_for_teams_honour_user_window_and_limit(tmp_path: Path) -> None:
    fixture = finished_fixture("NT")
    async with open_world(tmp_path / "notes.db", (fixture, 0, 1)) as bare:
        signal = await place(bare, fixture)
        foreign = await place(bare, fixture, user_id=OTHER)
        home = signal.fixture.home_team_id
        assert home is not None
        async with bare.sessions.begin() as session:
            for day in (30, 20, 10):
                await add_signal_note(
                    session, USER, signal.id, f"n{day}", created_at=NOW - timedelta(days=day)
                )
            await add_signal_note(session, OTHER, foreign.id, "boshqa", created_at=NOW)
        async with bare.sessions() as session:
            two = await user_notes_for_teams(session, USER, [home], since=LONG_AGO, limit=2)
            windowed = await user_notes_for_teams(
                session, USER, [home], since=NOW - timedelta(days=20), limit=5
            )
            nobody = await user_notes_for_teams(session, USER, [424242], since=LONG_AGO, limit=5)

    assert [n.text for n in two] == ["n10", "n20"]  # newest first, limited
    assert [n.text for n in windowed] == ["n10", "n20"]  # exactly at the bound is included
    assert nobody == []


async def test_settled_bets_are_read_by_settlement_time_half_open(tmp_path: Path) -> None:
    async with open_world(tmp_path / "bets.db") as bare:
        start = datetime(2026, 10, 20, 19, 0, tzinfo=UTC)
        end = start + timedelta(days=1)
        async with bare.sessions.begin() as session:
            await ensure_user(session, USER, timezone="Asia/Tashkent")
            await ensure_user(session, OTHER, timezone="Asia/Tashkent")
            rows = [
                (USER, start - timedelta(seconds=1), "-1"),  # before
                (USER, start, "-2"),  # first instant: in
                (USER, end - timedelta(seconds=1), "-3"),  # last instant: in
                (USER, end, "-4"),  # exclusive end: out
                (OTHER, start, "-5"),  # another user
            ]
            for user, settled_at, profit in rows:
                bet = await create_bet(
                    session,
                    user_id=user,
                    signal_id=None,
                    odds_taken=2.0,
                    stake=Decimal(10),
                    placed_at=settled_at - timedelta(hours=1),
                )
                await settle_bet(
                    session,
                    bet.id,
                    user_id=user,
                    outcome=Outcome.LOSS,
                    profit=Decimal(profit),
                    settled_at=settled_at,
                )
            await create_bet(  # still open: no settlement, no profit
                session,
                user_id=USER,
                signal_id=None,
                odds_taken=2.0,
                stake=Decimal(10),
                placed_at=start + timedelta(hours=1),
            )
        async with bare.sessions() as session:
            found = await bets_settled_between(session, USER, start=start, end=end)

    assert [bet.profit for bet in found] == [Decimal(-2), Decimal(-3)]
