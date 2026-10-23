"""T6: a week in the life of the installation, through the real dispatcher and the real notifier.

Real pipeline services over the seeded SQLite, the scripted API-Football double (HTTP is
covered by the respx tests), a scripted Gemini and the production ``TelegramNotifier`` writing
to the same recording Telegram session. Money is compared with an independent hand
computation (``t6_oracle``: Fractions, no ``app.models``).
"""

from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal

import pytest
from aiogram.methods import EditMessageText, SendMessage

from app.bot import texts
from app.bot.callbacks import ResultCb
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
    BetDraft,
    DateSpec,
    ReportRequest,
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


async def test_day_one_a_single_digest_a_double_tap_and_a_recalculation_diff(week: Week) -> None:
    w = week
    await w.settings(USER, **PERMISSIVE)
    summary = await jobs(w).run(JOB_REFRESH)
    assert summary is None  # the wrapper never raises or returns
    requests_after_refresh = len(w.provider.calls)
    assert requests_after_refresh > 0

    await tick(w, 30)
    w.sync(USER)
    pushed = w.pushed(USER)
    assert pushed[0].text.startswith("☀️ <b>Kunlik tahlil</b>")
    cards = [m for m in pushed if "🎯 Tanlov" in m.text]
    assert len(cards) == 10
    assert all("Salbiy omil ko'rinmaydi." in m.text for m in cards)  # Gemini notes arrived
    assert w.gemini.requests == 1  # one LLM request for the whole batch

    # another tick the same minute and half an hour later: still exactly one digest
    await tick(w, 60)
    digests = [m for m in w.pushed(USER) if m.text.startswith("☀️ <b>Kunlik tahlil</b>")]
    assert len(digests) == 1

    # open a card and place the bet; the confirmation is tapped twice
    client = w.client(USER)
    card_id, bet_data = client.find_button(texts.BTN_BET)
    await client.press_data(bet_data, card_id)
    confirm_id, confirm_data = client.find_button("Yozish")
    await client.press_data(confirm_data, confirm_id)
    await client.press_data(confirm_data, confirm_id)  # the same tap delivered twice
    bets = await w.services.bets.list_bets(USER)
    assert len(bets) == 1
    assert bets[0].stake == Decimal(10)

    # the price falls: 🔄 shows the diff, and the same signal is not recomputed again at once
    w.provider.odds = [
        replace(o, odds=max(round(o.odds * 0.9, 3), 1.02)) if o.bookmaker == "1xbet" else o
        for o in w.provider.odds
    ]
    recalc_id, recalc_data = client.find_button(texts.BTN_RECALC)
    methods = await client.press_data(recalc_data, recalc_id)
    body = "\n".join(m.text for m in methods if isinstance(m, SendMessage | EditMessageText))
    assert "yangilandi" in body
    assert "Koeff.:" in body
    assert "→" in body


async def test_day_two_every_number_equals_the_hand_computation(week: Week) -> None:  # noqa: PLR0915
    w = week
    day = await day_one(w)
    assert len(day.signals) == 10
    await craft(w, day)

    # bets with their own stakes and prices; one is placed through the card
    everything = [*day.signals, *day.crafted.values()]
    stakes = {
        s.signal_id: (Decimal(10 + 5 * index), round(s.odds + 0.05 * (index % 3), 2))
        for index, s in enumerate(everything)
    }
    placed = {}
    for signal in everything:
        stake, taken = stakes[signal.signal_id]
        placed[signal.signal_id] = await w.services.bets.place_bet(
            USER, BetDraft(signal_id=signal.signal_id, odds_taken=taken, stake=stake)
        )

    # the evening: pre-match updates, kick-offs, matches end
    at(w, 21, 6, 30)
    await tick(w, 14 * 60)  # up to ~20:30 UTC
    w.sync(USER)

    # the user marks one finished match by hand, against what the API will say
    victim = next(s for s in day.signals if expected_mark(w, s) in {"win", "loss"})
    manual = ResultMark.LOST if expected_mark(w, victim) == "win" else ResultMark.WON
    client = w.client(USER)
    await client.send("/natijalar")
    listing = client.last_message_id
    mark_cb = ResultCb(
        action="mark",
        signal_id=victim.signal_id,
        value="l" if manual is ResultMark.LOST else "w",
        origin="l",
        page=0,
    ).pack()
    await client.press_data(mark_cb, listing)
    assert (await w.services.signals.get_signal(USER, victim.signal_id)).result is manual

    # 08:00 settlement the next morning (Tashkent) = 03:00 UTC; the API sends all the results
    at(w, 22, 3, 0)
    await jobs(w).run(JOB_SETTLE)
    notices_after_settle = len([m for m in w.pushed(USER) if "💰 Natija:" in m.text])
    await jobs(w).run(JOB_SETTLE)  # a second run changes nothing and sends nothing
    assert len([m for m in w.pushed(USER) if "💰 Natija:" in m.text]) == notices_after_settle

    # every signal settled as the independent oracle says; the manual mark wins over the API
    views = await w.services.signals.get_signals(USER, [s.signal_id for s in everything])
    by_id = {v.signal_id: v for v in views}
    for signal in everything:
        got = by_id[signal.signal_id].result
        want = manual.value if signal.signal_id == victim.signal_id else expected_mark(w, signal)
        assert got is not None, signal
        assert got.value == want, (signal.home_team, signal.market, signal.selection)
    assert day.crafted["half_win"] and by_id[day.crafted["half_win"].signal_id].result is (
        ResultMark.HALF_WON
    )
    assert by_id[day.crafted["half_loss"].signal_id].result is ResultMark.HALF_LOST
    # the extra-time match is judged on regular time (1-1): the home pick loses
    assert by_id[day.crafted["aet"].signal_id].result is ResultMark.LOST

    # each user bet pays what the oracle says (the bet's own price and stake)
    expected_profit = Decimal(0)
    for signal in everything:
        bet = placed[signal.signal_id]
        stake, taken = stakes[signal.signal_id]
        if signal.signal_id == victim.signal_id:
            parts = (
                [(oracle.Fraction(1), oracle.WIN)]
                if manual is ResultMark.WON
                else [(oracle.Fraction(1), oracle.LOSS)]
            )
        else:
            fixture = fixture_of(w, signal)
            parts = oracle.legs(signal.market, signal.selection, *score_of(w, fixture.ref))
        want = oracle.money(stake, parts, taken)
        got = (await w.services.bets.get_bet(USER, bet.bet_id)).profit
        assert got is not None
        assert got.quantize(Decimal("0.01")) == want.quantize(Decimal("0.01")), signal
        expected_profit += want

    bank = await w.services.bets.bank_summary(USER)
    assert bank.balance is not None
    assert abs(bank.balance - (Decimal(1000) + expected_profit)) <= Decimal("0.05")
    assert bank.totals.settled == len(everything)
    assert bank.open_bets == 0

    # the report: units of the signals, count of ✅ / ❌ (half results count as ✅ / ❌)
    marks = [by_id[s.signal_id].result for s in everything]
    report = await w.services.reports.report(USER, ReportRequest.all_time())
    assert report.settled == len(everything)
    assert report.won == sum(m in {ResultMark.WON, ResultMark.HALF_WON} for m in marks)
    assert report.lost == sum(m in {ResultMark.LOST, ResultMark.HALF_LOST} for m in marks)
    units = Decimal(0)
    for signal in everything:
        if signal.signal_id == victim.signal_id:
            continue  # a manual mark is worth +-1 unit at the recommended odds, below
        units += expected_units(w, signal)
    odds = victim.recommended_odds or victim.odds
    units += (
        victim.stake_units * (Decimal(str(odds)) - 1)
        if manual is ResultMark.WON
        else -victim.stake_units
    )
    assert abs(report.profit_units - units) <= Decimal("0.01")

    # day 2 morning: refresh (03:30) then the digest (04:00) starts with yesterday's tally
    await jobs(w).run(JOB_REFRESH)
    await tick(w, 60)
    w.sync(USER)
    todays = [m for m in w.pushed(USER) if m.text.startswith("☀️ <b>Kunlik tahlil</b>")]
    assert len(todays) == 2
    won = sum(m in {ResultMark.WON, ResultMark.HALF_WON} for m in marks)
    lost = sum(m in {ResultMark.LOST, ResultMark.HALF_LOST} for m in marks)
    tally = f"📆 <b>Kecha:</b> {len(everything)} ta signal · {won} ✅ · {lost} ❌"
    assert tally in todays[1].text


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
