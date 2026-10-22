"""T3 QA: repository risks against a database built by the real Alembic migrations.

Concurrency (two workers claiming the same recalc job), report-read rules, exact money and
the history/live double-counting guard of ``finished_fixtures``.
"""

import asyncio
import sqlite3
import threading
from collections.abc import AsyncIterator
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.engine import create_engine, create_session_factory
from app.db.enums import (
    Confidence,
    Outcome,
    RecalcKind,
    ResultSource,
    SignalSource,
    SignalStatus,
)
from app.db.repo import (
    SignalDraft,
    SignalResultRecord,
    Thresholds,
    bets_placed_between,
    claim_due_recalcs,
    complete_recalc,
    create_bet,
    enqueue_recalc,
    ensure_user,
    finished_fixtures,
    get_fixtures,
    list_bets,
    list_user_signals,
    release_recalc,
    report_signals,
    set_signal_status,
    settle_bet,
    settle_signal,
    signal_versions,
    signals_for_fixtures,
    unsettled_signals,
    upsert_live,
    upsert_signal,
)
from app.domain import (
    ExternalRef,
    Fixture,
    FixtureResult,
    FixtureStatus,
    League,
    Market,
    ResultStatus,
    Selection,
    Sport,
    Team,
)
from app.errors import DataError
from tests.domain.factories import KICKOFF, make_fixture

USER, OTHER = 501, 502
NOW = KICKOFF - timedelta(days=1)
THRESHOLDS = Thresholds(0.45, 0.05, 1.5, 3.0)
DAY = timedelta(days=1)


@pytest.fixture
async def factory(migrated_db: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions over a file database created by ``alembic upgrade head``."""
    engine: AsyncEngine = create_engine(migrated_db)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


def draft(fixture_id: int, **changes: object) -> SignalDraft:
    base = SignalDraft(
        user_id=USER,
        fixture_id=fixture_id,
        market=Market.match_result(),
        selection=Selection.HOME,
        odds=2.0,
        p_model=0.55,
        ev=0.10,
        confidence=Confidence.MEDIUM,
        stake_units=Decimal("1.0"),
        source=SignalSource.ANALYSIS,
        thresholds=THRESHOLDS,
        recommended_at=NOW,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


async def seed_signals(
    factory: async_sessionmaker[AsyncSession], count: int
) -> tuple[list[int], list[int]]:
    """``count`` fixtures with one signal and one due recalc task each."""
    async with factory() as session, session.begin():
        await ensure_user(session, USER, timezone="UTC")
        fixtures = [
            make_fixture(f"Home{index}", f"Away{index}", status=FixtureStatus.SCHEDULED)
            for index in range(count)
        ]
        await upsert_live(session, fixtures=fixtures)
        stored = await get_fixtures(session, [fixture.ref for fixture in fixtures])
        signal_ids, task_ids = [], []
        for fixture in fixtures:
            record = await upsert_signal(session, draft(stored[fixture.ref].id))
            signal_ids.append(record.id)
            task = await enqueue_recalc(
                session, record.id, kind=RecalcKind.PREMATCH, run_at=NOW - timedelta(hours=1)
            )
            task_ids.append(task.id)
    return signal_ids, task_ids


# --------------------------------------------------------------------------- recalc queue


async def test_concurrent_claimers_never_receive_the_same_job(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    _, task_ids = await seed_signals(factory, 24)

    async def claimer() -> list[int]:
        async with factory() as session, session.begin():
            tasks = await claim_due_recalcs(session, now=NOW, limit=6)
            return [task.id for task in tasks]

    outcomes = await asyncio.gather(*(claimer() for _ in range(8)), return_exceptions=True)
    claimed = [
        identifier for outcome in outcomes if isinstance(outcome, list) for identifier in outcome
    ]
    assert len(claimed) == len(set(claimed)), "a job was handed to two claimers"
    assert set(claimed) <= set(task_ids)
    errors = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert errors == [], f"claimers crashed instead of waiting for the write lock: {errors}"
    # Whatever was not claimed concurrently is still claimable: nothing is lost.
    async with factory() as session, session.begin():
        rest = [task.id for task in await claim_due_recalcs(session, now=NOW, limit=100)]
    assert set(claimed) | set(rest) == set(task_ids)
    assert not set(claimed) & set(rest)


async def test_a_claim_that_is_rolled_back_leaves_the_job_claimable(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    _, task_ids = await seed_signals(factory, 3)
    async with factory() as session:
        claimed = await claim_due_recalcs(session, now=NOW)
        assert len(claimed) == 3
        await session.rollback()  # the worker crashed before committing
    async with factory() as session, session.begin():
        again = await claim_due_recalcs(session, now=NOW)
    assert {task.id for task in again} == set(task_ids)


async def test_lease_boundary_and_stale_claimer(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    _, task_ids = await seed_signals(factory, 1)
    lease = timedelta(minutes=15)
    async with factory() as session, session.begin():
        (first_claim,) = await claim_due_recalcs(session, now=NOW, lease=lease)
    async with factory() as session, session.begin():
        # Exactly at the lease limit the first claim is still valid ...
        assert await claim_due_recalcs(session, now=NOW + lease, lease=lease) == []
    async with factory() as session, session.begin():
        # ... one second later it expired and a second worker takes the job over.
        taken = await claim_due_recalcs(
            session, now=NOW + lease + timedelta(seconds=1), lease=lease
        )
        assert [task.id for task in taken] == task_ids
    async with factory() as session, session.begin():
        # The slow first worker no longer owns the job: it can neither finish nor release it.
        assert first_claim.claimed_at is not None
        stale = first_claim.claimed_at
        assert not await complete_recalc(
            session, task_ids[0], now=NOW + 2 * lease, claimed_at=stale
        )
        assert not await release_recalc(session, task_ids[0], claimed_at=stale)
        assert taken[0].claimed_at is not None
        assert await complete_recalc(
            session, task_ids[0], now=NOW + 2 * lease, claimed_at=taken[0].claimed_at
        )
        assert await claim_due_recalcs(session, now=NOW + 10 * lease, lease=lease) == []


async def test_completed_jobs_are_never_claimed_again(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    await seed_signals(factory, 4)
    async with factory() as session, session.begin():
        for task in await claim_due_recalcs(session, now=NOW):
            assert task.claimed_at is not None
            assert await complete_recalc(session, task.id, now=NOW, claimed_at=task.claimed_at)
    async with factory() as session, session.begin():
        assert await claim_due_recalcs(session, now=NOW + 365 * DAY) == []


# --------------------------------------------------------------------------- signals


async def test_two_simultaneous_first_analyses_become_one_signal_with_two_versions(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session, session.begin():
        await ensure_user(session, USER, timezone="UTC")
        fixture = make_fixture(status=FixtureStatus.SCHEDULED)
        await upsert_live(session, fixtures=[fixture])
        fixture_id = (await get_fixtures(session, [fixture.ref]))[fixture.ref].id
    # Deterministic interleaving: B starts after A holds the write lock (A's INSERT is
    # flushed, not committed); A commits only once B has issued its own INSERT.
    first_insert_done = threading.Event()
    second_insert_started = threading.Event()
    bind = factory.kw["bind"].sync_engine

    @event.listens_for(bind, "before_cursor_execute")
    def watch(
        _conn: object, _cursor: object, statement: str, _params: object, _ctx: object, _many: bool
    ) -> None:
        if statement.startswith("INSERT INTO signals ") and first_insert_done.is_set():
            second_insert_started.set()

    try:
        async with factory() as first:
            await first.begin()
            await upsert_signal(first, draft(fixture_id, odds=2.0))
            first_insert_done.set()

            async def second() -> int:
                async with factory() as session, session.begin():
                    return (await upsert_signal(session, draft(fixture_id, odds=2.1))).version

            task = asyncio.create_task(second())
            assert await asyncio.to_thread(second_insert_started.wait, 10), "B never inserted"
            await first.commit()
            outcome = await asyncio.gather(task, return_exceptions=True)
    finally:
        event.remove(bind, "before_cursor_execute", watch)
    assert outcome == [2]


async def seed_report_matrix(session: AsyncSession) -> dict[tuple[str, str, bool], int]:
    """One signal per (source, status, recommended) combination, each on its own fixture."""
    await ensure_user(session, USER, timezone="UTC")
    await ensure_user(session, OTHER, timezone="UTC")
    combos = [
        (source, status, recommended)
        for source in (SignalSource.ANALYSIS, SignalSource.RETRO)
        for status in (SignalStatus.ACTIVE, SignalStatus.HIDDEN)
        for recommended in (True, False)
    ]
    fixtures = [
        make_fixture(f"H{index}", f"A{index}", kickoff=KICKOFF + timedelta(minutes=index))
        for index in range(len(combos) + 1)
    ]
    await upsert_live(session, fixtures=fixtures)
    stored = await get_fixtures(session, [fixture.ref for fixture in fixtures])
    ids: dict[tuple[str, str, bool], int] = {}
    for fixture, (source, status, recommended) in zip(fixtures, combos, strict=False):
        record = await upsert_signal(
            session,
            draft(
                stored[fixture.ref].id,
                source=source,
                recommended_at=NOW if recommended else None,
            ),
        )
        await set_signal_status(session, record.id, status)
        ids[(source.value, status.value, recommended)] = record.id
    other = await upsert_signal(session, draft(stored[fixtures[-1].ref].id, user_id=OTHER))
    ids[("other-user", "active", True)] = other.id
    return ids


async def test_report_reads_exclude_retro_and_include_hidden(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session, session.begin():
        ids = await seed_report_matrix(session)
    async with factory() as session:
        reported = {record.id for record in await report_signals(session, USER)}
        listed = {
            record.id
            for record in await list_user_signals(
                session, USER, start=KICKOFF - DAY, end=KICKOFF + DAY
            )
        }
        unsettled = {
            record.id
            for record in await unsettled_signals(session, kicked_off_before=KICKOFF + DAY)
        }
        fixture_ids = [record.fixture.id for record in await report_signals(session, USER)]
        for_fixtures = {
            record.id
            for record in await signals_for_fixtures(
                session, fixture_ids, statuses=tuple(SignalStatus)
            )
        }
    expected_report = {
        identifier
        for (source, _status, recommended), identifier in ids.items()
        if source == "analysis" and recommended
    }
    # Hidden-but-recommended signals count in reports; retro and never-recommended ones do not.
    assert reported == expected_report
    assert ids[("analysis", "hidden", True)] in reported
    assert not {ids[key] for key in ids if key[0] == "retro"} & reported
    assert not {ids[key] for key in ids if not key[2]} & reported
    assert ids[("other-user", "active", True)] not in reported
    # The interactive list hides hidden and retro ones, but keeps "shown, not recommended".
    assert listed == {
        identifier
        for (source, status, _recommended), identifier in ids.items()
        if source == "analysis" and status == "active"
    } - {ids[("other-user", "active", True)]}
    assert not {ids[key] for key in ids if key[0] == "retro"} & (unsettled | for_fixtures)
    # ``unsettled_signals`` is global (every user), so it also lists the other user's one.
    assert unsettled == expected_report | {ids[("other-user", "active", True)]}


async def test_reports_are_windowed_by_kickoff_half_open(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session, session.begin():
        await ensure_user(session, USER, timezone="UTC")
        fixtures = [make_fixture(f"H{i}", f"A{i}", kickoff=KICKOFF + i * DAY) for i in range(3)]
        await upsert_live(session, fixtures=fixtures)
        stored = await get_fixtures(session, [f.ref for f in fixtures])
        for fixture in fixtures:
            await upsert_signal(session, draft(stored[fixture.ref].id))
    async with factory() as session:
        window = await report_signals(session, USER, start=KICKOFF, end=KICKOFF + 2 * DAY)
        assert [r.fixture.fixture.kickoff for r in window] == [KICKOFF, KICKOFF + DAY]
        assert len(await report_signals(session, USER)) == 3


# --------------------------------------------------------------------------- money


async def test_money_stays_decimal_end_to_end_and_is_stored_as_text(
    factory: async_sessionmaker[AsyncSession], migrated_db: Path
) -> None:
    placed = datetime(2025, 2, 1, 12, 0, tzinfo=UTC)
    async with factory() as session, session.begin():
        await ensure_user(session, USER, timezone="UTC")
        for _ in range(10):
            bet = await create_bet(
                session,
                user_id=USER,
                signal_id=None,
                odds_taken=1.91,
                stake=Decimal("0.1"),
                placed_at=placed,
            )
        await settle_bet(
            session,
            bet.id,
            user_id=USER,
            outcome=Outcome.WIN,
            profit=Decimal("0.0910"),
            settled_at=placed,
        )
    async with factory() as session:
        bets = await bets_placed_between(session, USER, start=placed, end=placed + DAY)
        assert all(isinstance(b.stake, Decimal) for b in bets)
        assert sum((b.stake for b in bets), Decimal(0)) == Decimal("1.0000")  # float sum: 0.9999..
        assert isinstance(bets[-1].profit, Decimal)
        assert bets[-1].profit == Decimal("0.0910")
        assert [b.id for b in await list_bets(session, USER, limit=3)] == [
            b.id for b in reversed(bets[-3:])
        ]
    with closing(sqlite3.connect(migrated_db)) as connection:
        kinds = connection.execute("SELECT DISTINCT typeof(stake) FROM user_bets").fetchall()
    assert kinds == [("text",)]


@pytest.mark.parametrize("bad", [0.1, 1, "0.1", Decimal("NaN"), Decimal("Infinity")])
async def test_money_columns_refuse_non_decimal_values(
    factory: async_sessionmaker[AsyncSession], bad: object
) -> None:
    async with factory() as session:
        await ensure_user(session, USER, timezone="UTC")
        with pytest.raises(StatementError) as error:
            await create_bet(
                session,
                user_id=USER,
                signal_id=None,
                odds_taken=2.0,
                stake=bad,  # type: ignore[arg-type]
                placed_at=KICKOFF,
            )
        assert isinstance(error.value.orig, DataError)
        await session.rollback()


async def test_signal_stakes_and_profits_are_decimal(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session, session.begin():
        await ensure_user(session, USER, timezone="UTC")
        fixture = make_fixture(status=FixtureStatus.SCHEDULED)
        await upsert_live(session, fixtures=[fixture])
        fixture_id = (await get_fixtures(session, [fixture.ref]))[fixture.ref].id
        record = await upsert_signal(session, draft(fixture_id, stake_units=Decimal("0.3333")))
        settled = await settle_signal(
            session,
            SignalResultRecord(
                signal_id=record.id,
                outcome=Outcome.HALF_WIN,
                source=ResultSource.API,
                profit_units=Decimal("0.1667"),
                closing_odds=1.9,
                clv=0.02,
                user_note=None,
                settled_at=KICKOFF + DAY,
            ),
        )
    assert isinstance(record.stake_units, Decimal)
    assert record.stake_units == Decimal("0.3333")
    assert isinstance(settled.profit_units, Decimal)
    assert settled.profit_units == Decimal("0.1667")


# --------------------------------------------------------------------------- history vs live

FD_LEAGUE = League(
    ref=ExternalRef("football-data", "E0"), sport=Sport.FOOTBALL, name="Premier League"
)
LIVE_LEAGUE = League(ref=ExternalRef("api-football", "39"), sport=Sport.FOOTBALL, name="PL")


def club(source: str, key: str, name: str) -> Team:
    return Team(ref=ExternalRef(source, key), sport=Sport.FOOTBALL, name=name)


def pairing(  # noqa: PLR0913, PLR0917 - test builder
    source: str, identifier: str, home: Team, away: Team, kickoff: datetime, league: League
) -> Fixture:
    return Fixture(
        ref=ExternalRef(source, identifier),
        sport=Sport.FOOTBALL,
        league=league,
        season=2024,
        kickoff=kickoff,
        home=home,
        away=away,
        status=FixtureStatus.FINISHED,
    )


def won(fixture: Fixture, home: int, away: int) -> FixtureResult:
    return FixtureResult(
        fixture=fixture.ref, status=ResultStatus.FINISHED, home_score=home, away_score=away
    )


HOME_TEAM = club("football-data", "Man United", "Man United")
AWAY_TEAM = club("football-data", "Fulham", "Fulham")


async def test_a_match_known_from_history_and_live_is_counted_once(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Same club identity (via IdentityMap) from two sources: one row for the model."""
    history = pairing("football-data", "h1", HOME_TEAM, AWAY_TEAM, KICKOFF, FD_LEAGUE)
    live = pairing(
        "api-football", "1208021", HOME_TEAM, AWAY_TEAM, KICKOFF + timedelta(minutes=1), FD_LEAGUE
    )
    async with factory() as session, session.begin():
        await upsert_live(
            session, fixtures=[history, live], results=[won(history, 1, 0), won(live, 1, 0)]
        )
    window = {"leagues": [FD_LEAGUE.ref], "start": KICKOFF - DAY, "end": KICKOFF + DAY}
    async with factory() as session:
        default = await finished_fixtures(session, **window)
        live_first = await finished_fixtures(session, **window, preferred_sources=["api-football"])
        history_first = await finished_fixtures(
            session, **window, preferred_sources=["football-data", "api-football"]
        )
    assert [r.fixture.ref.source for r in default] == ["football-data"]
    assert [r.fixture.ref.source for r in live_first] == ["api-football"]
    assert [r.fixture.ref.source for r in history_first] == ["football-data"]


async def test_unmatched_live_team_keeps_its_own_identity_and_is_not_merged(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    history = pairing("football-data", "h1", HOME_TEAM, AWAY_TEAM, KICKOFF, FD_LEAGUE)
    stranger = club("api-football", "33", "Manchester United")  # not mapped: stays separate
    live = pairing("api-football", "1", stranger, AWAY_TEAM, KICKOFF, LIVE_LEAGUE)
    async with factory() as session, session.begin():
        await upsert_live(
            session, fixtures=[history, live], results=[won(history, 1, 0), won(live, 1, 0)]
        )
    async with factory() as session:
        rows = await finished_fixtures(
            session,
            leagues=[FD_LEAGUE.ref, LIVE_LEAGUE.ref],
            start=KICKOFF - DAY,
            end=KICKOFF + DAY,
        )
    assert len(rows) == 2  # honest duplicates beat a silent wrong merge


async def test_only_finished_scored_matches_feed_the_model(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    played = pairing("football-data", "a", HOME_TEAM, AWAY_TEAM, KICKOFF, FD_LEAGUE)
    other_home = club("football-data", "Chelsea", "Chelsea")
    abandoned = replace(
        pairing("football-data", "b", other_home, AWAY_TEAM, KICKOFF, FD_LEAGUE),
        status=FixtureStatus.ABANDONED,
    )
    async with factory() as session, session.begin():
        await upsert_live(
            session,
            fixtures=[played, abandoned],
            results=[
                won(played, 2, 1),
                FixtureResult(
                    fixture=abandoned.ref,
                    status=ResultStatus.ABANDONED,
                    home_score=1,
                    away_score=0,
                ),
            ],
        )
    async with factory() as session:
        rows = await finished_fixtures(
            session, leagues=[FD_LEAGUE.ref], start=KICKOFF - DAY, end=KICKOFF + DAY
        )
    assert [r.fixture.ref.external_id for r in rows] == ["a"]


async def test_a_pairing_played_twice_in_one_season_is_not_collapsed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    regular = pairing("api-football", "r", HOME_TEAM, AWAY_TEAM, KICKOFF, LIVE_LEAGUE)
    playoff = pairing("api-football", "p", HOME_TEAM, AWAY_TEAM, KICKOFF + 150 * DAY, LIVE_LEAGUE)
    async with factory() as session, session.begin():
        await upsert_live(
            session,
            fixtures=[regular, playoff],
            results=[won(regular, 1, 0), won(playoff, 0, 2)],
        )
    async with factory() as session:
        rows = await finished_fixtures(
            session, leagues=[LIVE_LEAGUE.ref], start=KICKOFF - DAY, end=KICKOFF + 200 * DAY
        )
    assert sorted(r.fixture.ref.external_id for r in rows) == ["p", "r"]


async def test_raw_database_counts_confirm_two_rows_one_model_row(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    history = pairing("football-data", "h1", HOME_TEAM, AWAY_TEAM, KICKOFF, FD_LEAGUE)
    live = pairing("api-football", "9", HOME_TEAM, AWAY_TEAM, KICKOFF, FD_LEAGUE)
    async with factory() as session, session.begin():
        await upsert_live(
            session, fixtures=[history, live], results=[won(history, 3, 3), won(live, 3, 3)]
        )
    async with factory() as session:
        stored = (await session.execute(text("SELECT count(*) FROM fixtures"))).scalar_one()
        teams = (await session.execute(text("SELECT count(*) FROM teams"))).scalar_one()
        model_rows = await finished_fixtures(
            session, leagues=[FD_LEAGUE.ref], start=KICKOFF - DAY, end=KICKOFF + DAY
        )
    assert (stored, teams, len(model_rows)) == (2, 2, 1)


async def test_signal_identity_is_user_fixture_market_line_selection(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session, session.begin():
        await ensure_user(session, USER, timezone="UTC")
        await ensure_user(session, OTHER, timezone="UTC")
        fixture = make_fixture(status=FixtureStatus.SCHEDULED)
        await upsert_live(session, fixtures=[fixture])
        fixture_id = (await get_fixtures(session, [fixture.ref]))[fixture.ref].id
        variants: list[dict[str, object]] = [
            {},
            {"market": Market.totals(2.5), "selection": Selection.OVER},
            {"market": Market.totals(2.5), "selection": Selection.UNDER},
            {"market": Market.totals(3.5), "selection": Selection.OVER},
            {"market": Market.asian_handicap(-0.5), "selection": Selection.HOME},
            {"market": Market.asian_handicap(0.5), "selection": Selection.HOME},
            {"market": Market.asian_handicap(-0.5), "selection": Selection.AWAY},
            {"user_id": OTHER},
        ]
        first_pass = [await upsert_signal(session, draft(fixture_id, **v)) for v in variants]
        second_pass = [
            await upsert_signal(session, draft(fixture_id, odds=2.2, **v)) for v in variants
        ]
        third = await upsert_signal(session, draft(fixture_id, odds=2.4))
    assert len({record.id for record in first_pass}) == len(variants)  # all distinct keys
    assert [r.id for r in second_pass] == [r.id for r in first_pass]  # re-analysis: same rows
    assert third.id == first_pass[0].id
    assert [r.version for r in second_pass] == [2] * len(variants)
    assert third.version == 3
    async with factory() as session:
        history = await signal_versions(session, first_pass[0].id)
    assert [(v.version, v.odds) for v in history] == [(1, 2.0), (2, 2.2), (3, 2.4)]
