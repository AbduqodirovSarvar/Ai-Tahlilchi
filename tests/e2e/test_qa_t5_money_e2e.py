"""QA (T5 bot): money correctness of the bets UI through the real dispatcher + REAL services.

Every number is checked against an independent hand computation: bets are settled by hand
(``ResultService.mark_result``), so the outcome of each one is known before the test starts.
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.bot import texts
from app.bot.callbacks import BankCb, BetCb, CardCb
from app.services.interfaces import BetDraft, ResultMark
from tests.e2e.bot_client import Client, answers, shown
from tests.e2e.real_world import ADMIN, RealBot

pytestmark = pytest.mark.slow

LIMIT = Decimal(100)


async def _press_mark(admin: Client, bet_id: int, code: str) -> str:
    """Press a result button of an open bet; returns the screen the bot answers with."""
    methods = await admin.press_data(
        BankCb(action="mark", id=bet_id, value=code).pack(), message_id=50
    )
    return "\n".join(shown(methods))


async def _card_bet_press(rb: RealBot, signal_id: int) -> str:
    methods = await rb.client().press_data(
        CardCb(action="bet", signal_id=signal_id).pack(), message_id=60
    )
    return "\n".join(shown(methods))


# --------------------------------------------------------------------------- loss limit
async def test_loss_limit_blocks_exactly_at_the_limit_and_not_one_unit_before(
    real_bot: RealBot,
) -> None:
    rb = real_bot
    await rb.settings(bank=Decimal(1000), unit=Decimal(10), daily_loss_limit=LIMIT)
    signals = await rb.signals(distinct=3)
    first, second, target = signals[0], signals[1], signals[2]
    bets = [await rb.bet(first, 99), await rb.bet(second, 1)]
    rb.finish_matches()

    await _press_mark(rb.client(), bets[0].bet_id, "l")
    below = await rb.services.bets.bank_summary(ADMIN)
    assert below.loss_today == Decimal(99)
    assert not below.loss_limit_reached
    assert "Zarar limitiga yetdingiz" not in await _card_bet_press(rb, target.signal_id)

    await _press_mark(rb.client(), bets[1].bet_id, "l")
    at_limit = await rb.services.bets.bank_summary(ADMIN)
    assert at_limit.loss_today == LIMIT
    assert at_limit.loss_limit_reached
    asked = await _card_bet_press(rb, target.signal_id)
    assert "Zarar limitiga yetdingiz" in asked
    assert "bugun 100" in asked


async def test_force_past_the_limit_is_an_explicit_button_that_records_nothing_by_itself(
    real_bot: RealBot,
) -> None:
    rb = real_bot
    await rb.settings(bank=Decimal(1000), unit=Decimal(10), daily_loss_limit=Decimal(50))
    signals = await rb.signals(distinct=2)
    bet = await rb.bet(signals[0], 50)
    rb.finish_matches()
    await _press_mark(rb.client(), bet.bet_id, "l")
    assert (await rb.services.bets.bank_summary(ADMIN)).loss_limit_reached

    admin = rb.client()
    await admin.press_data(CardCb(action="bet", signal_id=signals[1].signal_id).pack(), 60)
    assert not await rb.services.bets.list_bets(ADMIN, open_only=True)
    forced = await admin.press_data(
        BetCb(action="force", signal_id=signals[1].signal_id).pack(), 61
    )

    assert texts.BET_LIMIT_FORCED in "\n".join(shown(forced))
    assert not await rb.services.bets.list_bets(ADMIN, open_only=True)


@pytest.mark.parametrize(
    ("zone", "marked_utc", "now_utc", "today_loss", "week_loss"),
    [
        # 03:30 UTC Oct 24 is 23:30 Oct 23 in New York: yesterday there, today in Tashkent.
        ("America/New_York", (2026, 10, 24, 3, 30), (2026, 10, 24, 5, 0), 0, 99),
        ("Asia/Tashkent", (2026, 10, 24, 3, 30), (2026, 10, 24, 5, 0), 99, 99),
        # Sunday 23:30 in New York is Monday morning in Tashkent: another week there.
        ("America/New_York", (2026, 10, 26, 3, 30), (2026, 10, 26, 5, 0), 0, 0),
        ("Asia/Tashkent", (2026, 10, 26, 3, 30), (2026, 10, 26, 5, 0), 99, 99),
        # Auckland (UTC+13 in October) is already on Sunday 25 Oct when UTC is still the 24th;
        # 12:30 UTC the next day is Monday 26 Oct there: a new day and a new week.
        ("Pacific/Auckland", (2026, 10, 24, 12, 30), (2026, 10, 24, 20, 0), 99, 99),
        ("Pacific/Auckland", (2026, 10, 24, 12, 30), (2026, 10, 25, 12, 30), 0, 0),
    ],
)
async def test_loss_windows_follow_the_users_time_zone(
    real_bot: RealBot,
    zone: str,
    marked_utc: tuple[int, int, int, int, int],
    now_utc: tuple[int, int, int, int, int],
    today_loss: int,
    week_loss: int,
) -> None:
    rb = real_bot
    await rb.settings(timezone=zone, bank=Decimal(1000), unit=Decimal(10))
    signal = (await rb.signals())[0]
    bet = await rb.bet(signal, 99)
    rb.env.clock.now = datetime(*marked_utc, tzinfo=UTC)
    await _press_mark(rb.client(), bet.bet_id, "l")
    rb.env.clock.now = datetime(*now_utc, tzinfo=UTC)

    bank = await rb.services.bets.bank_summary(ADMIN)

    assert (bank.loss_today, bank.loss_this_week) == (Decimal(today_loss), Decimal(week_loss))


async def test_a_winning_day_offsets_losses_net_not_gross(real_bot: RealBot) -> None:
    rb = real_bot
    await rb.settings(bank=Decimal(1000), unit=Decimal(10), daily_loss_limit=Decimal(60))
    first, second = (await rb.signals(distinct=2))[:2]
    lost, won = await rb.bet(first, 50), await rb.bet(second, 30, odds=3.0)
    rb.finish_matches()
    await _press_mark(rb.client(), lost.bet_id, "l")
    await _press_mark(rb.client(), won.bet_id, "w")

    bank = await rb.services.bets.bank_summary(ADMIN)

    # -50 + 30 * (3 - 1) = +10 for the day: no loss at all, so no limit
    assert bank.loss_today == 0
    assert not bank.loss_limit_reached


# --------------------------------------------------------------------------- balance and P/L
async def test_overview_balance_and_profit_match_a_hand_computation(real_bot: RealBot) -> None:
    rb = real_bot
    await rb.settings(bank=Decimal(1000), unit=Decimal(10))
    signals = await rb.signals(distinct=4)
    won = await rb.bet(signals[0], 10, odds=2.5)  # +15
    lost = await rb.bet(signals[1], 7)  # -7
    void = await rb.bet(signals[2], 5)  # 0, not counted as risked
    open_bet = await rb.bet(signals[3], 400)  # never settled: must not touch the balance
    rb.finish_matches()
    admin = rb.client()
    # the open bet's match has started but nothing marked it: it stays open
    for bet, code in ((won, "w"), (lost, "l"), (void, "v")):
        await _press_mark(admin, bet.bet_id, code)

    text = "\n".join(shown(await admin.send(texts.MENU_BETS)))

    assert "Balans: <b>1 008</b>" in text  # 1000 + 15 - 7
    assert "Foyda/zarar (yopilganlar): <b>+8</b>" in text
    assert "stavkaga nisbatan +47.1%" in text  # 8 / (10 + 7), void excluded
    assert "Yopilgan: 3 ta · ✅ 1 · ❌ 1 · ↩️ 1" in text
    bank = await rb.services.bets.bank_summary(ADMIN)
    assert bank.balance == Decimal(1008)
    assert bank.open_bets == 1
    assert open_bet.stake == 400


async def test_double_tap_on_a_result_button_settles_once(real_bot: RealBot) -> None:
    rb = real_bot
    await rb.settings(bank=Decimal(1000), unit=Decimal(10))
    signal = (await rb.signals())[0]
    bet = await rb.bet(signal, 10, odds=2.0)
    rb.finish_matches()
    admin = rb.client()
    data = BankCb(action="mark", id=bet.bet_id, value="w").pack()

    first = await admin.press_data(data, 50)
    second = await admin.press_data(data, 50)

    assert texts.BANK_MARKED.format(result=texts.RESULT_LABELS[ResultMark.WON]) in shown(first)[0]
    assert answers(second)[0].show_alert  # "bet not found": already settled
    bank = await rb.services.bets.bank_summary(ADMIN)
    assert bank.balance == Decimal(1010)  # +10 once, not +20


async def test_marking_one_bet_leaves_bets_on_other_signals_open(real_bot: RealBot) -> None:
    rb = real_bot
    await rb.settings(bank=Decimal(1000), unit=Decimal(10))
    first, second = (await rb.signals(distinct=2))[:2]
    one, two = await rb.bet(first, 10), await rb.bet(second, 20)
    rb.finish_matches()

    await _press_mark(rb.client(), one.bet_id, "w")

    views = {bet.bet_id: bet for bet in await rb.services.bets.list_bets(ADMIN)}
    assert views[one.bet_id].outcome is ResultMark.WON
    assert views[two.bet_id].outcome is None
    assert views[two.bet_id].profit is None


async def test_marking_a_signal_settles_every_bet_on_it_and_the_screen_says_so(
    real_bot: RealBot,
) -> None:
    rb = real_bot
    await rb.settings(bank=Decimal(1000), unit=Decimal(10))
    signal = (await rb.signals())[0]
    low = await rb.bet(signal, 10, odds=2.0)
    high = await rb.bet(signal, 30, odds=1.5)
    rb.finish_matches()
    admin = rb.client()

    screen = "\n".join(
        shown(await admin.press_data(BankCb(action="bet", id=low.bet_id).pack(), 50))
    )
    await _press_mark(admin, low.bet_id, "w")

    assert "barcha stavkalarga" in screen  # the user is warned before pressing
    views = {bet.bet_id: bet for bet in await rb.services.bets.list_bets(ADMIN)}
    assert views[low.bet_id].profit == Decimal(10)  # 10 * (2.0 - 1)
    assert views[high.bet_id].profit == Decimal(15)  # 30 * (1.5 - 1), at ITS own odds


# --------------------------------------------------------------------------- cashout units
async def test_cashout_of_a_default_stake_bet_is_units_times_the_unit(real_bot: RealBot) -> None:
    rb = real_bot
    await rb.settings(bank=Decimal(1000), unit=Decimal(100))
    signal = (await rb.signals())[0]
    assert signal.stake_units == 1
    bet = await rb.services.bets.place_bet(ADMIN, BetDraft(signal_id=signal.signal_id))
    assert bet.stake == Decimal(100)
    rb.finish_matches()
    admin = rb.client()

    await _press_mark(admin, bet.bet_id, "c")
    await admin.send("0.8")

    (settled,) = await rb.services.bets.list_bets(ADMIN)
    assert settled.outcome is ResultMark.CASHOUT
    assert settled.profit == Decimal(80)


@pytest.mark.xfail(
    strict=True,
    reason="QA F-2: the cash-out prompt says 'unit' but a bet with a custom stake gets "
    "stake * units / signal.stake_units, not units * the user's unit",
)
async def test_cashout_units_mean_the_users_unit_whatever_the_stake(real_bot: RealBot) -> None:
    rb = real_bot
    await rb.settings(bank=Decimal(1000), unit=Decimal(100))
    signal = (await rb.signals())[0]
    bet = await rb.bet(signal, 250)  # 2.5 units of a 100 unit
    rb.finish_matches()
    admin = rb.client()
    await _press_mark(admin, bet.bet_id, "c")
    await admin.send("0.8")

    (settled,) = await rb.services.bets.list_bets(ADMIN)

    assert settled.profit == Decimal(80)  # "+0.8 unit" of a 100 unit


# --------------------------------------------------------------------------- idempotency, size
async def test_two_simultaneous_taps_on_the_bet_prompt_record_one_bet(real_bot: RealBot) -> None:
    rb = real_bot
    await rb.settings(bank=Decimal(1000), unit=Decimal(10))
    signal = (await rb.signals())[0]
    admin = rb.client()
    data = BetCb(action="ok", signal_id=signal.signal_id).pack()

    await asyncio.gather(admin.press_data(data, 70), admin.press_data(data, 70))

    assert len(await rb.services.bets.list_bets(ADMIN)) == 1


@pytest.mark.xfail(
    strict=True,
    reason="QA F-6: the overview totals only the latest 100 bets (LIST_LIMIT) while the balance "
    "counts all of them, and the text does not say so (BANK_RECENT is never used)",
)
async def test_overview_profit_line_covers_every_bet_not_just_the_latest_hundred(
    real_bot: RealBot,
) -> None:
    rb = real_bot
    await rb.settings(bank=Decimal(1000), unit=Decimal(10))
    signal = (await rb.signals())[0]
    for _ in range(105):
        await rb.bet(signal, 1)
    rb.finish_matches()
    await rb.mark(signal, ResultMark.WON)

    text = "\n".join(shown(await rb.client().send(texts.MENU_BETS)))

    assert "Balans: <b>1 105</b>" in text
    assert "Foyda/zarar (yopilganlar): <b>+105</b>" in text
