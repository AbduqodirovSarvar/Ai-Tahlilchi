"""🎟 Mening stavkalarim (plan §6.9, F7.5): overview, bet lists, manual settling, bank chart.

The overview (menu button or ``/bank``) is the home screen; every other screen edits the same
message and leads back to it. Bets are looked up again whenever a button is pressed, so a bet
that was settled meanwhile is reported instead of acted on. A manual result is set on the
bet's signal (contract: ``ResultService.mark_result``), which also settles every other bet
on that signal. Chart rendering blocks, so it runs in a worker thread.
"""

import asyncio
from decimal import Decimal, InvalidOperation
from typing import Final

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message

from app.bot import texts
from app.bot.bank_ui import (
    CASHOUT_CODE,
    MARK_CODES,
    back_keyboard,
    bet_keyboard,
    chart_points,
    format_bet,
    format_open_list,
    format_overview,
    format_settled_list,
    open_keyboard,
    overview_keyboard,
    setup_keyboard,
)
from app.bot.callbacks import BankCb
from app.bot.charts import bank_chart
from app.bot.draft import StaleDraftError
from app.bot.flow import FlowContext, FlowContextMiddleware
from app.bot.formatters import money
from app.bot.handlers.filters import TYPED_INPUT
from app.bot.keyboards import markup_of, single_button
from app.bot.messaging import (
    answer_callback,
    callback_message,
    drop_keyboard,
    edit_html,
    send_html,
)
from app.bot.parsing import InputError, parse_number
from app.bot.states import BetFlow
from app.services.errors import NotFoundError, ValidationFailedError
from app.services.interfaces import BetView, ResultMark, ResultMarkRequest

BANK_COMMAND: Final = "bank"
LIST_LIMIT: Final = 100
"""Bets read for the overview, lists and totals (the service's own cap)."""
CASHOUT_KEY: Final = "cashout"
MAX_CASHOUT_UNITS: Final = Decimal(100)


def _message_of(callback: CallbackQuery) -> Message:
    message = callback_message(callback)
    if message is None:  # pragma: no cover - Telegram hides only very old messages
        raise StaleDraftError
    return message


async def _bets(flow: FlowContext, *, open_only: bool = False) -> tuple[BetView, ...]:
    return await flow.services.bets.list_bets(flow.user_id, open_only=open_only, limit=LIST_LIMIT)


async def _open_bet(flow: FlowContext, bet_id: int) -> BetView:
    """The user's open bet.

    Raises:
        NotFoundError: settled meanwhile, or not the user's.
    """
    for bet in await _bets(flow, open_only=True):
        if bet.bet_id == bet_id:
            return bet
    raise NotFoundError("bet", bet_id)


# --------------------------------------------------------------------------- overview
async def _overview(
    flow: FlowContext, notice: str | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    settings = await flow.settings()
    bank = await flow.services.bets.bank_summary(flow.user_id)
    bets = await _bets(flow)
    text = format_overview(bank, bets, settings, settings.zone)
    open_count = sum(1 for bet in bets if bet.outcome is None)
    return (f"{notice}\n\n{text}" if notice else text), overview_keyboard(open_count)


async def show_overview(message: Message, flow: FlowContext) -> None:
    """🎟 Mening stavkalarim / ``/bank``."""
    await _clear_cashout(flow)
    text, markup = await _overview(flow)
    await send_html(message, text, markup)


async def _edit_overview(
    callback: CallbackQuery, flow: FlowContext, notice: str | None = None
) -> None:
    text, markup = await _overview(flow, notice)
    await edit_html(_message_of(callback), text, markup)


async def on_home(callback: CallbackQuery, flow: FlowContext) -> None:
    """⬅️ Stavkalarim."""
    await answer_callback(callback)
    await _clear_cashout(flow)
    await _edit_overview(callback, flow)


# --------------------------------------------------------------------------- lists
async def on_open(callback: CallbackQuery, flow: FlowContext) -> None:
    """🟡 Ochiq: the open bets, each a button."""
    bets = await _bets(flow, open_only=True)
    zone = await flow.zone()
    await answer_callback(callback)
    await _clear_cashout(flow)
    await edit_html(_message_of(callback), format_open_list(bets, zone), open_keyboard(bets))


async def on_settled(callback: CallbackQuery, flow: FlowContext) -> None:
    """✅ Yopilgan: the latest settled bets with their profit."""
    bets = await _bets(flow)
    await answer_callback(callback)
    await edit_html(_message_of(callback), format_settled_list(bets), back_keyboard())


async def on_bet(callback: CallbackQuery, callback_data: BankCb, flow: FlowContext) -> None:
    """An open bet: result buttons."""
    bet = await _open_bet(flow, callback_data.id)
    await answer_callback(callback)
    await edit_html(_message_of(callback), format_bet(bet), bet_keyboard(bet.bet_id))


async def on_setup(callback: CallbackQuery) -> None:
    """⚙️ Bank va limitlar: the settings editors for bank, unit and loss limits."""
    await answer_callback(callback)
    await edit_html(_message_of(callback), texts.BANK_SETUP_TITLE, setup_keyboard())


# --------------------------------------------------------------------------- manual result
async def _mark(flow: FlowContext, bet: BetView, mark: ResultMark, units: Decimal | None) -> None:
    if bet.signal_id is None:
        raise ValidationFailedError("signal_id", "the bet has no signal")
    request = ResultMarkRequest(signal_id=bet.signal_id, mark=mark, cashout_profit_units=units)
    await flow.services.results.mark_result(flow.user_id, request)


async def on_mark(callback: CallbackQuery, callback_data: BankCb, flow: FlowContext) -> None:
    """A result button of an open bet; cash-out asks for the amount first."""
    bet = await _open_bet(flow, callback_data.id)
    if bet.signal_id is None:
        await answer_callback(callback, texts.BANK_NO_SIGNAL, alert=True)
        return
    if callback_data.value == CASHOUT_CODE:
        await _ask_cashout(callback, flow, bet)
        return
    mark = MARK_CODES.get(callback_data.value)
    if mark is None:
        raise StaleDraftError
    try:
        await _mark(flow, bet, mark, None)
    except ValidationFailedError:
        await answer_callback(callback, texts.BANK_MARK_REFUSED, alert=True)
        return
    await answer_callback(callback)
    notice = texts.BANK_MARKED.format(result=texts.RESULT_LABELS[mark])
    await _edit_overview(callback, flow, notice)


async def _ask_cashout(callback: CallbackQuery, flow: FlowContext, bet: BetView) -> None:
    message = _message_of(callback)
    await answer_callback(callback)
    await flow.state.update_data(
        {CASHOUT_KEY: {"bet_id": bet.bet_id, "message_id": message.message_id}}
    )
    await flow.state.set_state(BetFlow.cashout_input)
    await edit_html(
        message,
        texts.BANK_CASHOUT_PROMPT,
        markup_of([[single_button(texts.BTN_BANK_HOME, BankCb(action="open"))]]),
    )


async def _clear_cashout(flow: FlowContext) -> None:
    """Forget a pending cash-out amount (the user went elsewhere)."""
    if await flow.state.get_state() != BetFlow.cashout_input.state:
        return
    data = await flow.state.get_data()
    data.pop(CASHOUT_KEY, None)
    await flow.state.set_state(None)
    await flow.state.set_data(data)


def _parse_units(text: str) -> Decimal:
    """The typed cash-out result in units.

    Raises:
        InputError: not a number, or beyond any plausible result.
    """
    number = parse_number(text, example="0.8")
    try:
        value = Decimal(str(number))
    except InvalidOperation:  # pragma: no cover - parse_number already rejects non-finite
        raise InputError(texts.BANK_CASHOUT_RANGE) from None
    if abs(value) > MAX_CASHOUT_UNITS:
        raise InputError(texts.BANK_CASHOUT_RANGE)
    return value


async def on_cashout_text(message: Message, flow: FlowContext) -> None:
    """The typed cash-out result: settle the bet and show the overview."""
    stored = (await flow.state.get_data()).get(CASHOUT_KEY)
    if not isinstance(stored, dict) or not isinstance(stored.get("bet_id"), int):
        raise StaleDraftError
    units = _parse_units(message.text or "")
    bet = await _open_bet(flow, stored["bet_id"])
    try:
        await _mark(flow, bet, ResultMark.CASHOUT, units)
    except ValidationFailedError:
        await send_html(message, texts.BANK_MARK_REFUSED)
        return
    await _clear_cashout(flow)
    if message.bot is not None:
        await drop_keyboard(message.bot, message.chat.id, stored.get("message_id"))
    notice = texts.BANK_MARKED.format(result=texts.RESULT_LABELS[ResultMark.CASHOUT])
    text, markup = await _overview(flow, notice)
    await send_html(message, text, markup)


# --------------------------------------------------------------------------- chart
async def on_chart(callback: CallbackQuery, flow: FlowContext) -> None:
    """📈 Bank grafigi: a PNG of the balance at the end of every settled day."""
    bank = await flow.services.bets.bank_summary(flow.user_id)
    if not bank.history:
        await answer_callback(callback, texts.BANK_NO_CHART, alert=True)
        return
    settings = await flow.settings()
    await answer_callback(callback)
    png = await asyncio.to_thread(bank_chart, chart_points(bank), settings.bank)
    caption = texts.BANK_CHART_CAPTION.format(
        first=money(bank.history[0].balance), last=money(bank.history[-1].balance)
    )
    await _message_of(callback).answer_photo(
        BufferedInputFile(png, filename="bank.png"), caption=caption, parse_mode=ParseMode.HTML
    )


# --------------------------------------------------------------------------- router
def create_router() -> Router:
    """Build a fresh router (an aiogram router can be attached to only one dispatcher)."""
    router = Router(name="bank")
    router.message.middleware(FlowContextMiddleware())
    router.callback_query.middleware(FlowContextMiddleware())
    router.message.register(show_overview, F.text == texts.MENU_BETS)
    router.message.register(show_overview, Command(BANK_COMMAND))
    router.message.register(on_cashout_text, TYPED_INPUT, StateFilter(BetFlow.cashout_input))
    for action, handler in (
        ("home", on_home),
        ("open", on_open),
        ("settled", on_settled),
        ("bet", on_bet),
        ("mark", on_mark),
        ("chart", on_chart),
        ("setup", on_setup),
    ):
        router.callback_query.register(handler, BankCb.filter(F.action == action))
    return router
