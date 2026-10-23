"""T6 findings pinned as strict xfails: each XPASSes (and fails the suite) when it is fixed.

Deterministic on purpose: query counts, not wall-clock time.
"""

from decimal import Decimal

import pytest
from sqlalchemy import event

from app.db.schema import UserBetRow
from app.services.interfaces import BetDraft, DateSpec
from tests.e2e.t6_world import PERMISSIVE, USER, Week, jobs, open_week, tick

pytestmark = pytest.mark.slow


@pytest.fixture
async def week(history_template, tmp_path, bot, session):  # type: ignore[no-untyped-def]
    async with open_week(history_template, tmp_path, bot, session) as opened:
        await opened.settings(USER, **PERMISSIVE)
        await jobs(opened).run("refresh")
        await tick(opened, 30)
        yield opened


async def add_bets(w: Week, count: int) -> list[int]:
    """``count`` more open bets on the user's signals (written straight into the table)."""
    signals = await w.services.signals.list_signals(USER, DateSpec.next_days(3))
    async with w.env.sessions.begin() as session:
        rows = [
            UserBetRow(
                user_id=USER,
                signal_id=signals[index % len(signals)].signal_id,
                odds_taken=2.0,
                stake=Decimal(10),
                placed_at=w.clock(),
            )
            for index in range(count)
        ]
        session.add_all(rows)
        await session.flush()
        return [row.id for row in rows]


class Counter:
    def __init__(self, engine) -> None:  # type: ignore[no-untyped-def]
        self.count = 0
        event.listen(engine.sync_engine, "before_cursor_execute", self._hit)

    def _hit(self, *_args: object) -> None:
        self.count += 1


async def statements_for_get_bet(w: Week, bets: int) -> int:
    ids = await add_bets(w, bets)
    counter = Counter(w.env.engine)
    await w.services.bets.get_bet(USER, ids[0])
    return counter.count


@pytest.mark.xfail(
    strict=True,
    reason="T6 finding (performance): RealBetService.get_bet loads EVERY bet of the user and "
    "then one signal per bet (N+1: src/app/services/real/signals.py _bets/get_bet), so opening "
    "one bet costs O(bets) queries: ~3.6 ms per bet, 3.6 s at a thousand bets",
)
async def test_opening_one_bet_costs_a_constant_number_of_queries(week: Week) -> None:
    few = await statements_for_get_bet(week, 5)
    many = await statements_for_get_bet(week, 60)
    assert many <= few + 2, (few, many)


@pytest.mark.xfail(
    strict=True,
    reason="T6 finding (performance): the CSV bets export has the same N+1 (a get_signal per bet)",
)
async def test_the_bets_export_costs_a_constant_number_of_queries(week: Week) -> None:
    await add_bets(week, 5)
    counter = Counter(week.env.engine)
    await week.services.reports.export_bets(USER)
    few = counter.count
    await add_bets(week, 55)
    counter.count = 0
    await week.services.reports.export_bets(USER)
    assert counter.count <= few + 2, (few, counter.count)


async def test_the_findings_fixture_is_alive(week: Week) -> None:
    placed = await week.services.bets.place_bet(USER, BetDraft(signal_id=1))
    assert placed.stake == Decimal(10)


@pytest.mark.xfail(
    strict=True,
    reason="T6 finding (hardening): aiogram's FSM middleware runs BEFORE the whitelist, so every "
    "distinct stranger leaves a lock in SimpleEventIsolation._locks for the life of the process "
    "(src/app/bot/dispatcher.py build_dispatcher: whitelist is registered after the FSM one)",
)
async def test_strangers_leave_nothing_behind_in_the_process(week: Week) -> None:
    from tests.e2e.bot_client import Client  # noqa: PLC0415

    isolation = week.dispatcher.fsm.events_isolation
    before = len(getattr(isolation, "_locks", {}))
    for stranger in range(3_000, 3_060):
        await Client(week.dispatcher, week.bot, week.session, stranger).send("/start")
    assert len(getattr(isolation, "_locks", {})) == before
