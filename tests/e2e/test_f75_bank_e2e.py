"""🎟 Mening stavkalarim through the real dispatcher over the fake services (F7.5)."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.methods import SendPhoto

from app.bot import texts
from app.bot.callbacks import BankCb, BetCb, CardCb
from app.bot.formatters import money
from app.bot.states import BetFlow
from app.services.fake import create_fake_services
from app.services.interfaces import (
    AnalysisQuery,
    BankView,
    BetDraft,
    DateSpec,
    ResultMark,
    ResultMarkRequest,
    Services,
    SignalView,
)
from app.services.report_math import bank_totals
from tests.bot.fakes import ADMIN_ID, RecordingSession
from tests.e2e.bot_client import Client, answers, make_dispatcher, shown
from tests.e2e.conftest import SECOND_ADMIN_ID
from tests.services.contract_suite import START, MutableClock


class World:
    """A client over fake services whose clock the test moves."""

    def __init__(self, bot: Bot, session: RecordingSession) -> None:
        self.clock = MutableClock(now=START)
        self.services: Services = create_fake_services(clock=self.clock)
        dispatcher = make_dispatcher(frozenset({ADMIN_ID, SECOND_ADMIN_ID}), self.services)
        self.admin = Client(dispatcher, bot, session, ADMIN_ID)

    async def settings(self, **changes: object) -> None:
        current = await self.services.settings.get_settings(ADMIN_ID)
        await self.services.settings.update_settings(ADMIN_ID, replace(current, **changes))  # type: ignore[arg-type]

    async def signal(self) -> SignalView:
        result = await self.services.analysis.analyze(
            ADMIN_ID,
            AnalysisQuery(dates=DateSpec.next_days(3), min_probability=0.3, min_ev=0.0),
        )
        return result.signals[0]

    async def bet_on_started_match(self, stake: int = 10_000, odds: float = 2.0) -> int:
        """Place a bet, then let the match start (not finish): the bet stays open."""
        signal = await self.signal()
        bet = await self.services.bets.place_bet(
            ADMIN_ID,
            BetDraft(signal_id=signal.signal_id, odds_taken=odds, stake=Decimal(stake)),
        )
        self.clock.now = signal.kickoff + timedelta(minutes=30)
        return bet.bet_id


@pytest.fixture
def world(bot: Bot, session: RecordingSession) -> World:
    return World(bot, session)


# --------------------------------------------------------------------------- overview
async def test_overview_without_bets_or_bank(world: World) -> None:
    methods = await world.admin.send(texts.MENU_BETS)

    (text,) = shown(methods)
    assert texts.BETS_BANK_UNSET in text
    assert texts.BETS_EMPTY in text
    assert texts.BANK_UNIT_UNSET in text
    assert "Yopilgan: 0 ta" in text
    for label in (texts.BTN_BANK_SETTLED, texts.BTN_BANK_CHART, texts.BTN_BANK_SETUP):
        assert world.admin.has_button(label)


async def test_bank_command_is_the_same_screen(world: World) -> None:
    menu = shown(await world.admin.send(texts.MENU_BETS))
    command = shown(await world.admin.send("/bank"))

    assert menu == command


async def test_overview_shows_bank_unit_open_bets_and_limits(world: World) -> None:
    await world.settings(
        bank=Decimal(1_000_000),
        unit=Decimal(10_000),
        daily_loss_limit=Decimal(50_000),
        weekly_loss_limit=Decimal(150_000),
    )
    await world.bet_on_started_match()

    (text,) = shown(await world.admin.send(texts.MENU_BETS))

    assert "Balans: <b>1 000 000</b>" in text
    assert "Unit: <b>10 000</b>" in text
    assert "Ochiq stavkalar: 1" in text
    assert "bugun 0/50 000, hafta 0/150 000" in text
    assert world.admin.has_button(texts.BTN_BANK_OPEN.format(count=1))


async def test_other_users_do_not_see_my_bets(world: World) -> None:
    await world.bet_on_started_match()
    other = Client(world.admin.dispatcher, world.admin.bot, world.admin.session, SECOND_ADMIN_ID)

    (text,) = shown(await other.send(texts.MENU_BETS))

    assert texts.BETS_EMPTY in text
    assert "Ochiq stavkalar: 0" in text


# --------------------------------------------------------------------------- lists
async def test_open_list_and_bet_screen(world: World) -> None:
    await world.bet_on_started_match()
    await world.admin.send(texts.MENU_BETS)

    listing = shown(await world.admin.press("Ochiq"))
    assert "Ochiq stavkalar</b> — 1 ta" in listing[0]
    bet_screen = shown(await world.admin.press(" · 2.00"))[0]

    assert "@ <b>2.00</b>" in bet_screen
    for mark in (ResultMark.WON, ResultMark.LOST, ResultMark.VOID):
        assert world.admin.has_button(texts.RESULT_LABELS[mark])
    assert world.admin.has_button(texts.BTN_MARK_CASHOUT)


async def test_settled_list_is_empty_then_shows_results(world: World) -> None:
    await world.bet_on_started_match()
    await world.admin.send(texts.MENU_BETS)
    assert shown(await world.admin.press(texts.BTN_BANK_SETTLED)) == [texts.BANK_SETTLED_EMPTY]

    await world.admin.press(texts.BTN_BANK_HOME)
    await world.admin.press("Ochiq")
    await world.admin.press(" · 2.00")
    await world.admin.press(texts.RESULT_LABELS[ResultMark.WON])
    methods = await world.admin.press(texts.BTN_BANK_SETTLED)

    (text,) = shown(methods)
    assert "Yopilgan stavkalar" in text
    assert "✅ O'tdi +" in text


# --------------------------------------------------------------------------- marking
@pytest.mark.parametrize(
    ("mark", "profit"),
    [
        (ResultMark.WON, None),
        (ResultMark.LOST, Decimal(-10_000)),
        (ResultMark.VOID, Decimal(0)),
    ],
)
async def test_manual_result_settles_the_bet(
    world: World, mark: ResultMark, profit: Decimal | None
) -> None:
    await world.settings(bank=Decimal(100_000), unit=Decimal(10_000))
    await world.bet_on_started_match()
    await world.admin.send(texts.MENU_BETS)
    await world.admin.press("Ochiq")
    await world.admin.press(" · 2.00")

    methods = await world.admin.press(texts.RESULT_LABELS[mark])

    (bet,) = await world.services.bets.list_bets(ADMIN_ID)
    assert bet.outcome is mark
    assert bet.profit is not None
    if profit is None:
        assert bet.profit > 0  # a win pays (odds - 1) times the stake
    else:
        assert bet.profit == profit
    text = shown(methods)[0]
    assert texts.BANK_MARKED.format(result=texts.RESULT_LABELS[mark]) in text
    assert f"Balans: <b>{100_000 + int(bet.profit):,}".replace(",", " ") in text
    assert "Ochiq stavkalar: 0" in text


async def test_result_cannot_be_marked_before_the_match(world: World) -> None:
    signal = await world.signal()
    await world.services.bets.place_bet(
        ADMIN_ID,
        BetDraft(signal_id=signal.signal_id, odds_taken=2.0, stake=Decimal(1000)),
    )
    await world.admin.send(texts.MENU_BETS)
    await world.admin.press("Ochiq")
    await world.admin.press(" · 2.00")

    methods = await world.admin.press(texts.RESULT_LABELS[ResultMark.WON])

    assert answers(methods)[0].text == texts.BANK_MARK_REFUSED
    assert (await world.services.bets.list_bets(ADMIN_ID))[0].outcome is None


async def test_cashout_asks_for_the_payout_and_validates(world: World) -> None:
    await world.settings(bank=Decimal(100_000), unit=Decimal(10_000))
    await world.bet_on_started_match()
    (open_bet,) = await world.services.bets.list_bets(ADMIN_ID)
    await world.admin.send(texts.MENU_BETS)
    await world.admin.press("Ochiq")
    await world.admin.press(" · 2.00")

    prompt = shown(await world.admin.press(texts.BTN_MARK_CASHOUT))
    assert prompt == [texts.BANK_CASHOUT_PROMPT.format(stake=money(open_bet.stake))]
    assert await world.admin.state() == BetFlow.cashout_input.state
    assert "abc" in shown(await world.admin.send("abc"))[0]
    assert "-5" in shown(await world.admin.send("-5"))[0]  # a payout is never negative
    assert shown(await world.admin.send("1" + "0" * 15)) == [texts.BANK_CASHOUT_RANGE]
    assert await world.admin.state() == BetFlow.cashout_input.state

    methods = await world.admin.send("6 000")

    (bet,) = await world.services.bets.list_bets(ADMIN_ID)
    assert bet.outcome is ResultMark.CASHOUT
    assert bet.profit == Decimal(6000) - open_bet.stake  # payout minus stake, in money
    assert "Cashout" in shown(methods)[0]
    assert await world.admin.state() is None


async def test_leaving_the_cashout_step_forgets_it(world: World) -> None:
    await world.bet_on_started_match()
    await world.admin.send(texts.MENU_BETS)
    await world.admin.press("Ochiq")
    await world.admin.press(" · 2.00")
    await world.admin.press(texts.BTN_MARK_CASHOUT)

    await world.admin.press(texts.BTN_BANK_HOME)

    assert await world.admin.state() is None


async def test_a_bet_settled_meanwhile_is_not_found(world: World) -> None:
    bet_id = await world.bet_on_started_match()
    await world.admin.send(texts.MENU_BETS)
    await world.admin.press("Ochiq")
    message_id, data = world.admin.find_button(" · 2.00")
    assert BankCb.unpack(data).id == bet_id
    (bet,) = await world.services.bets.list_bets(ADMIN_ID)
    assert bet.signal_id is not None
    await world.services.results.mark_result(
        ADMIN_ID, ResultMarkRequest(signal_id=bet.signal_id, mark=ResultMark.LOST)
    )

    methods = await world.admin.press_data(data, message_id)

    assert texts.NOT_FOUND in [a.text for a in answers(methods)] + shown(methods)


async def test_forged_mark_code_is_stale(world: World) -> None:
    bet_id = await world.bet_on_started_match()
    await world.admin.send(texts.MENU_BETS)
    message_id = world.admin.last_message_id

    methods = await world.admin.press_data(
        BankCb(action="mark", id=bet_id, value="x").pack(), message_id
    )

    assert "Bu tugma eskirgan" in (answers(methods)[0].text or "")


# --------------------------------------------------------------------------- chart
async def test_chart_needs_a_bank_and_a_settled_bet(world: World) -> None:
    await world.admin.send(texts.MENU_BETS)

    methods = await world.admin.press(texts.BTN_BANK_CHART)

    assert answers(methods)[0].text == texts.BANK_NO_CHART
    assert not [m for m in methods if isinstance(m, SendPhoto)]


async def test_chart_is_sent_as_a_png_photo(world: World) -> None:
    await world.settings(bank=Decimal(100_000), unit=Decimal(10_000))
    await world.bet_on_started_match()
    await world.admin.send(texts.MENU_BETS)
    await world.admin.press("Ochiq")
    await world.admin.press(" · 2.00")
    await world.admin.press(texts.RESULT_LABELS[ResultMark.WON])
    await world.admin.press(texts.BTN_BANK_CHART)

    photos = [m for m in world.admin.session.requests if isinstance(m, SendPhoto)]
    assert len(photos) == 1
    assert photos[0].photo.data.startswith(b"\x89PNG")  # type: ignore[union-attr]
    (bet,) = await world.services.bets.list_bets(ADMIN_ID)
    assert bet.profit is not None
    balance = f"{100_000 + int(bet.profit):,}".replace(",", " ")
    assert f"{balance} → {balance}" in (photos[0].caption or "")


# --------------------------------------------------------------------------- setup
async def test_setup_opens_the_settings_editors(world: World) -> None:
    await world.admin.send(texts.MENU_BETS)

    setup = shown(await world.admin.press(texts.BTN_BANK_SETUP))
    assert setup == [texts.BANK_SETUP_TITLE]
    editor = shown(await world.admin.press(texts.SETTINGS_BUTTONS["unit"]))

    assert texts.SETTINGS_PROMPTS["unit"] in editor[0]


# --------------------------------------------------------------------------- loss limit
async def card_message(world: World) -> tuple[int, int]:
    await world.admin.send("/tahlil 7kun 50% ev3%")
    await world.admin.press(texts.BTN_START)
    message_id, data = world.admin.find_button(texts.BTN_BET)
    return message_id, CardCb.unpack(data).signal_id


async def test_past_the_loss_limit_the_bet_needs_a_second_confirmation(world: World) -> None:
    await world.settings(bank=Decimal(1_000_000), unit=Decimal(10_000))
    message_id, signal_id = await card_message(world)
    world.services.bets.bank_summary = AsyncMock(  # type: ignore[method-assign]
        return_value=limit_reached_sync()
    )

    methods = await world.admin.press_data(
        CardCb(action="bet", signal_id=signal_id).pack(), message_id
    )

    text = shown(methods)[0]
    assert "Zarar limitiga yetdingiz" in text
    assert "bugun 60 000" in text
    assert not world.admin.has_button(texts.BTN_BET_CUSTOM)

    methods = await world.admin.press(texts.BTN_BANK_FORCE)

    assert texts.BET_LIMIT_FORCED in shown(methods)[0]
    assert texts.BET_PROMPT.split("{")[0] in shown(methods)[1]
    assert world.admin.has_button(texts.BTN_BET_CUSTOM)


def limit_reached_sync() -> BankView:
    return BankView(
        balance=None,
        unit=None,
        month_profit=Decimal(0),
        open_bets=0,
        history=(),
        loss_today=Decimal(60_000),
        loss_this_week=Decimal(60_000),
        loss_limit_reached=True,
    )


async def test_declining_after_the_limit_warning_writes_nothing(world: World) -> None:
    await world.settings(bank=Decimal(1_000_000), unit=Decimal(10_000))
    message_id, signal_id = await card_message(world)
    world.services.bets.bank_summary = AsyncMock(  # type: ignore[method-assign]
        return_value=limit_reached_sync()
    )
    await world.admin.press_data(CardCb(action="bet", signal_id=signal_id).pack(), message_id)

    methods = await world.admin.press(texts.BTN_BET_CANCEL)

    assert shown(methods) == [texts.BET_CANCELLED]
    assert await world.services.bets.list_bets(ADMIN_ID) == ()


async def test_force_button_of_a_vanished_signal_explains(world: World) -> None:
    await world.admin.send(texts.MENU_BETS)
    message_id = world.admin.last_message_id

    methods = await world.admin.press_data(
        BetCb(action="force", signal_id=99_999).pack(), message_id
    )

    assert texts.NOT_FOUND in [a.text for a in answers(methods)] + shown(methods)


# --------------------------------------------------------------------------- pure
def test_totals_ignore_void_stakes_for_the_yield() -> None:
    rows = [
        (ResultMark.WON, Decimal(100), Decimal(100)),
        (ResultMark.LOST, Decimal(100), Decimal(-100)),
        (ResultMark.VOID, Decimal(500), Decimal(0)),
        (ResultMark.CASHOUT, Decimal(100), Decimal(20)),
        (None, Decimal(70), None),
    ]

    totals = bank_totals(rows)

    assert (totals.settled, totals.won, totals.lost, totals.void, totals.cashout) == (4, 1, 1, 1, 1)
    assert totals.profit == Decimal(20)
    assert totals.staked == Decimal(300)
    assert totals.yield_rate == pytest.approx(20 / 300)
    assert bank_totals([]).yield_rate is None
