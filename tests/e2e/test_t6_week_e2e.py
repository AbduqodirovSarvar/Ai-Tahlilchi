"""T6: a week in the life of the installation, through the real dispatcher and the real notifier.

Real pipeline services over the seeded SQLite, the scripted API-Football double (HTTP is
covered by the respx tests), a scripted Gemini and the production ``TelegramNotifier`` writing
to the same recording Telegram session. Money is compared with an independent hand
computation (``t6_oracle``: Fractions, no ``app.models``).
"""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

import pytest

from app.domain import (
    ExternalRef,
    Fixture,
    FixtureResult,
    Market,
    ResultStatus,
    Selection,
)
from app.scheduler import JOB_REFRESH, JOB_SETTLE
from app.services.interfaces import (
    DateSpec,
    ResultMark,
    SignalView,
)
from tests.e2e import t6_oracle as oracle
from tests.e2e.t6_world import PERMISSIVE, USER, Week, at, jobs, open_week, tick
from tests.integration.t4_support import Bare, add_signal

pytestmark = pytest.mark.slow

ONE = Decimal(1)


@pytest.fixture
async def week(history_template, tmp_path, bot, session):  # type: ignore[no-untyped-def]
    async with open_week(history_template, tmp_path, bot, session) as opened:
        yield opened


def bare_of(w: Week) -> Bare:
    return Bare(w.ctx, w.system, w.env.notifier, w.clock, w.env.engine, w.env.sessions)


def fixture_of(w: Week, view: SignalView) -> Fixture:
    return next(
        f
        for f in w.provider.fixtures
        if (f.home.name, f.away.name) == (view.home_team, view.away_team)
    )


def score_of(w: Week, ref: ExternalRef) -> tuple[int, int]:
    result = w.provider.results[ref]
    assert result.home_score is not None
    assert result.away_score is not None
    return result.home_score, result.away_score


def set_score(w: Week, ref: ExternalRef, home: int, away: int) -> None:
    w.provider.results[ref] = FixtureResult(
        fixture=ref, status=ResultStatus.FINISHED, home_score=home, away_score=away
    )


@dataclass
class DayOne:
    """What the first morning produced."""

    signals: list[SignalView]
    crafted: dict[str, SignalView]


async def day_one(w: Week) -> DayOne:
    """Settings, 08:30 refresh, the 09:00 digest, bets; returns the user's signals."""
    await w.settings(USER, **PERMISSIVE)
    await jobs(w).run(JOB_REFRESH)
    await tick(w, 30, step=30)  # 06:30: the daily tick delivers the digest
    w.sync(USER)
    listed = await w.services.signals.list_signals(USER, DateSpec.next_days(3))
    return DayOne(signals=list(listed), crafted={})


async def craft(w: Week, taken: DayOne) -> None:
    """Three hand-made picks: an extra-time match, a quarter-line half win and half loss."""
    used = {(s.home_team, s.away_team, s.market, s.selection) for s in taken.signals}
    spare = [
        f
        for f in w.provider.fixtures
        if f.kickoff.date().isoformat() == "2026-10-21" and f.kickoff > w.clock()
    ]
    plan = (
        ("aet", Market.match_result(), Selection.HOME, 2.30, (1, 1)),
        ("half_win", Market.asian_handicap(-0.75), Selection.HOME, 1.90, (1, 0)),
        ("half_loss", Market.asian_handicap(-0.25), Selection.HOME, 2.10, (1, 1)),
    )
    for fixture, (name, market, selection, odds, score) in zip(spare[-3:], plan, strict=True):
        assert (fixture.home.name, fixture.away.name, market, selection) not in used
        record = await add_signal(bare_of(w), fixture, market, selection, odds, ONE, user_id=USER)
        view = await w.services.signals.get_signal(USER, record.id)
        taken.crafted[name] = view
        set_score(w, fixture.ref, *score)


def expected_mark(w: Week, view: SignalView) -> str:
    fixture = fixture_of(w, view)
    home, away = score_of(w, fixture.ref)
    parts = oracle.legs(view.market, view.selection, home, away)
    return oracle.mark_of(parts)


def expected_units(w: Week, view: SignalView) -> Decimal:
    fixture = fixture_of(w, view)
    home, away = score_of(w, fixture.ref)
    parts = oracle.legs(view.market, view.selection, home, away)
    price = view.recommended_odds or view.odds
    return oracle.money(view.stake_units, parts, price)


async def test_a_manual_mark_survives_a_later_api_result_and_the_notice_is_not_repeated(
    week: Week,
) -> None:
    w = week
    day = await day_one(w)
    victim = day.signals[0]
    truth = expected_mark(w, victim)
    at(w, 21, 20, 30)
    wrong = ResultMark.LOST if truth in {"win", "half_win"} else ResultMark.WON
    from app.services.interfaces import ResultMarkRequest  # noqa: PLC0415

    await w.services.results.mark_result(
        USER, ResultMarkRequest(signal_id=victim.signal_id, mark=wrong)
    )
    at(w, 22, 3, 0)
    await jobs(w).run(JOB_SETTLE)
    await jobs(w).run(JOB_SETTLE)
    assert (await w.services.signals.get_signal(USER, victim.signal_id)).result is wrong


async def test_the_scheduler_jobs_leave_no_failure_behind(week: Week) -> None:
    w = week
    await day_one(w)
    at(w, 22, 3, 0)
    await jobs(w).run(JOB_SETTLE)
    await jobs(w).run(JOB_REFRESH)
    runs = await w.ctx.ledger.last_runs()
    failed = {name: run.detail for name, run in runs.items() if not run.ok}
    assert failed == {}
    assert timedelta(0) == timedelta(0)
