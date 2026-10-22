"""✅ Stavka qildim and 🎟 Mening stavkalarim (bets wiring of the cards) end to end."""

import asyncio
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.methods import EditMessageReplyMarkup, EditMessageText

from app.bot import texts
from app.bot.callbacks import BetCb, CardCb
from app.bot.handlers.bets import prompt_token
from app.services.interfaces import BetDraft, BetView, Services
from tests.bot.fakes import ADMIN_ID
from tests.e2e.bot_client import (
    Client,
    YieldingSession,
    answers,
    make_dispatcher,
    sent,
    shown,
    telegram_html_problems,
)

UNIT = Decimal("10000")


async def set_unit(services: Services, unit: Decimal | None = UNIT) -> None:
    settings = await services.settings.get_settings(ADMIN_ID)
    bank = None if unit is None else unit * 100
    await services.settings.update_settings(ADMIN_ID, replace(settings, unit=unit, bank=bank))


async def open_prompt(admin: Client) -> tuple[int, int]:
    """Analyse, tap ✅ on a card; return (prompt message id, signal id)."""
    await admin.send("/tahlil 7kun 50% ev3% 10ta")
    await admin.press(texts.BTN_START)
    card_id, data = admin.find_button(texts.BTN_BET)
    signal_id = CardCb.unpack(data).signal_id
    methods = await admin.press_data(data, card_id)
    assert len(answers(methods)) == 1
    return admin.last_message_id, signal_id


async def bets_of(services: Services) -> tuple[BetView, ...]:
    return await services.bets.list_bets(ADMIN_ID)


# --------------------------------------------------------------------------- prompt
async def test_prompt_shows_signal_odds_and_the_recommended_stake(
    admin: Client, services: Services
) -> None:
    await set_unit(services)

    prompt_id, _ = await open_prompt(admin)

    text = admin.texts[prompt_id]
    assert "Signal koeffitsienti: <b>" in text
    assert "unit)</b>" in text  # money with units
    assert admin.has_button("Yozish")
    assert admin.has_button(texts.BTN_BET_CUSTOM)
    assert telegram_html_problems(text) == []


async def test_prompt_without_a_unit_asks_for_a_typed_stake(
    admin: Client, services: Services
) -> None:
    await set_unit(services, None)

    await open_prompt(admin)

    assert not admin.has_button("Yozish")
    assert admin.has_button(texts.BTN_BET_CUSTOM)


async def test_retro_signal_cannot_be_bet_on(admin: Client) -> None:
    await admin.send("/tahlil 15.10-19.10 30% ev0%")
    await admin.press(texts.BTN_START)
    card_id, detail = admin.find_button(texts.BTN_DETAIL)
    retro_id = CardCb.unpack(detail).signal_id

    methods = await admin.press_data(CardCb(action="bet", signal_id=retro_id).pack(), card_id)

    (answer,) = answers(methods)
    assert answer.text == texts.VALIDATION_BY_FIELD["signal_id"]
    assert answer.show_alert is True


# --------------------------------------------------------------------------- confirm with defaults
async def test_confirming_records_the_bet_with_the_signals_numbers(
    admin: Client, services: Services
) -> None:
    await set_unit(services)
    prompt_id, signal_id = await open_prompt(admin)

    methods = await admin.press("Yozish")

    (edit,) = [m for m in methods if isinstance(m, EditMessageText)]
    assert edit.message_id == prompt_id
    assert edit.text.startswith("🎟 <b>Stavka yozildi</b>")
    assert edit.reply_markup is None
    (bet,) = await bets_of(services)
    signal = await services.signals.get_signal(ADMIN_ID, signal_id)
    assert bet.signal_id == signal_id
    assert bet.odds_taken == signal.odds
    assert bet.stake == UNIT * signal.stake_units


async def test_double_tap_on_confirm_records_one_bet(admin: Client, services: Services) -> None:
    await set_unit(services)
    prompt_id, signal_id = await open_prompt(admin)
    data = BetCb(action="ok", signal_id=signal_id).pack()
    real = services.bets.place_bet
    drafts: list[BetDraft] = []

    async def spy(user_id: int, draft: BetDraft) -> BetView:
        drafts.append(draft)
        return await real(user_id, draft)

    services.bets.place_bet = spy  # type: ignore[method-assign]

    await admin.press_data(data, prompt_id)
    await admin.press_data(data, prompt_id)

    assert len(await bets_of(services)) == 1
    assert [d.client_token for d in drafts] == [prompt_token(signal_id, prompt_id)] * 2


async def test_concurrent_double_tap_records_one_bet() -> None:
    session = YieldingSession()
    bot = Bot(token="42:TEST", session=session)
    dispatcher = make_dispatcher(frozenset({ADMIN_ID}))
    services: Services = dispatcher["services"]
    admin = Client(dispatcher, bot, session, ADMIN_ID)
    await set_unit(services)
    prompt_id, signal_id = await open_prompt(admin)
    data = BetCb(action="ok", signal_id=signal_id).pack()

    await asyncio.gather(admin.press_data(data, prompt_id), admin.press_data(data, prompt_id))

    assert len(await bets_of(services)) == 1


async def test_two_prompts_are_two_deliberate_bets(admin: Client, services: Services) -> None:
    await set_unit(services)
    first_prompt, signal_id = await open_prompt(admin)
    card_id, data = admin.find_button(texts.BTN_BET)
    await admin.press_data(data, card_id)
    second_prompt = admin.last_message_id
    ok = BetCb(action="ok", signal_id=signal_id).pack()

    await admin.press_data(ok, first_prompt)
    await admin.press_data(ok, second_prompt)

    assert first_prompt != second_prompt
    assert len(await bets_of(services)) == 2


async def test_confirming_without_a_unit_explains_what_to_do(
    admin: Client, services: Services
) -> None:
    await set_unit(services, None)
    prompt_id, signal_id = await open_prompt(admin)

    methods = await admin.press_data(BetCb(action="ok", signal_id=signal_id).pack(), prompt_id)

    assert shown(methods) == [texts.VALIDATION_BY_FIELD["stake"]]
    assert await bets_of(services) == ()


async def test_loss_limit_warning_follows_the_confirmation(
    admin: Client, services: Services
) -> None:
    await set_unit(services)
    prompt_id, signal_id = await open_prompt(admin)
    bank = await services.bets.bank_summary(ADMIN_ID)
    services.bets.bank_summary = AsyncMock(  # type: ignore[method-assign]
        return_value=replace(bank, loss_limit_reached=True)
    )

    methods = await admin.press_data(BetCb(action="ok", signal_id=signal_id).pack(), prompt_id)

    (edit,) = [m for m in methods if isinstance(m, EditMessageText)]
    assert edit.text.endswith(texts.BET_LOSS_LIMIT)


async def test_cancel_closes_the_prompt(admin: Client, services: Services) -> None:
    await set_unit(services)
    prompt_id, _ = await open_prompt(admin)

    methods = await admin.press(texts.BTN_BET_CANCEL)

    assert shown(methods) == [texts.BET_CANCELLED]
    assert admin.markups[prompt_id] is None
    assert await bets_of(services) == ()


# --------------------------------------------------------------------------- typed values
async def test_typed_odds_and_stake_are_recorded(admin: Client, services: Services) -> None:
    prompt_id, signal_id = await open_prompt(admin)

    methods = await admin.press(texts.BTN_BET_CUSTOM)

    assert shown(methods) == [texts.BET_INPUT_PROMPT]
    assert await admin.state() == "BetFlow:value_input"
    methods = await admin.send("1.95 50 000")
    (bet,) = await bets_of(services)
    assert (bet.signal_id, bet.odds_taken, bet.stake) == (signal_id, 1.95, Decimal("50000"))
    assert shown(methods)[0].startswith("🎟 <b>Stavka yozildi</b>")
    assert "@ <b>1.95</b>" in shown(methods)[0]
    assert await admin.state() is None
    assert any(isinstance(m, EditMessageReplyMarkup) and m.message_id == prompt_id for m in methods)


async def test_typed_odds_without_stake_use_the_recommended_stake(
    admin: Client, services: Services
) -> None:
    await set_unit(services)
    await open_prompt(admin)
    await admin.press(texts.BTN_BET_CUSTOM)

    await admin.send("2,10")

    (bet,) = await bets_of(services)
    assert bet.odds_taken == 2.1
    assert bet.stake > 0


async def test_bad_typed_values_keep_the_step_open(admin: Client, services: Services) -> None:
    await set_unit(services, None)
    await open_prompt(admin)
    await admin.press(texts.BTN_BET_CUSTOM)

    garbage = await admin.send("abc")
    too_low = await admin.send("0.5 1000")
    no_stake = await admin.send("1.9")

    assert "Format" in shown(garbage)[0] or "son emas" in shown(garbage)[0]
    assert "1.01" in shown(too_low)[0]
    assert shown(no_stake) == [texts.VALIDATION_BY_FIELD["stake"]]
    assert await admin.state() == "BetFlow:value_input"
    assert await bets_of(services) == ()
    await admin.send("1.9 20000")
    assert len(await bets_of(services)) == 1


async def test_typed_values_cannot_start_while_another_flow_is_open(admin: Client) -> None:
    await open_prompt(admin)
    prompt_id = admin.last_message_id
    await admin.send(texts.MENU_ANALYSIS)  # a wizard is now open
    state_before = await admin.state()

    methods = await admin.press_data(BetCb(action="custom", signal_id=1).pack(), prompt_id)

    (answer,) = answers(methods)
    assert answer.text == texts.BET_FINISH_FLOW_FIRST
    assert answer.show_alert is True
    assert await admin.state() == state_before


async def test_cancel_while_typing_leaves_the_step(admin: Client) -> None:
    await open_prompt(admin)
    await admin.press(texts.BTN_BET_CUSTOM)

    await admin.press(texts.BTN_BET_CANCEL)

    assert await admin.state() is None


async def test_bekor_leaves_the_typing_step(admin: Client) -> None:
    await open_prompt(admin)
    await admin.press(texts.BTN_BET_CUSTOM)

    methods = await admin.send("/bekor")

    assert shown(methods) == [texts.CANCELLED]
    assert await admin.state() is None


async def test_menu_buttons_are_not_taken_for_typed_odds(admin: Client, services: Services) -> None:
    await open_prompt(admin)
    await admin.press(texts.BTN_BET_CUSTOM)

    methods = await admin.send(texts.MENU_ANALYSIS)

    assert "Sana oralig'i" in shown(methods)[0]
    assert await bets_of(services) == ()


async def test_typed_values_after_a_restart_are_not_a_bet(
    admin: Client, services: Services
) -> None:
    await open_prompt(admin)
    await admin.press(texts.BTN_BET_CUSTOM)
    admin.dispatcher.storage.storage.clear()  # type: ignore[attr-defined]

    methods = await admin.send("1.9 1000")

    assert shown(methods) == [texts.UNKNOWN_TEXT]
    assert await bets_of(services) == ()


# --------------------------------------------------------------------------- the list
async def test_bets_list_shows_open_bets_and_the_bank(admin: Client, services: Services) -> None:
    await set_unit(services)
    prompt_id, signal_id = await open_prompt(admin)
    await admin.press_data(BetCb(action="ok", signal_id=signal_id).pack(), prompt_id)

    methods = await admin.send(texts.MENU_BETS)

    (text,) = shown(methods)
    assert "🎟 <b>Mening stavkalarim</b>" in text
    assert "Balans: <b>1 000 000</b>" in text
    assert "Ochiq stavkalar: 1" in text
    assert text.count("• ") == 1
    assert telegram_html_problems(text) == []


async def test_bets_list_empty_state(admin: Client) -> None:
    methods = await admin.send(texts.MENU_BETS)

    (text,) = shown(methods)
    assert texts.BETS_EMPTY in text
    assert texts.BETS_BANK_UNSET in text


async def test_bet_buttons_always_get_exactly_one_answer(admin: Client, services: Services) -> None:
    await set_unit(services)
    prompt_id, signal_id = await open_prompt(admin)
    for action in ("custom", "ok", "cancel", "ok"):
        methods = await admin.press_data(
            BetCb(action=action, signal_id=signal_id).pack(), prompt_id
        )
        assert len(answers(methods)) == 1, action
        assert not [m for m in sent(methods) if m.text == texts.UNEXPECTED_ERROR]


@pytest.mark.parametrize("action", ["ok", "custom", "cancel"])
async def test_bet_buttons_of_an_unknown_signal_do_not_crash(admin: Client, action: str) -> None:
    prompt_id, _ = await open_prompt(admin)

    methods = await admin.press_data(BetCb(action=action, signal_id=999_999).pack(), prompt_id)

    assert len(answers(methods)) == 1
    assert texts.UNEXPECTED_ERROR not in shown(methods)
