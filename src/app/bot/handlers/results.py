"""Result marking (plan 6.13 A, F8.1): card buttons, ``/natijalar`` and the 💬 note.

Every button names a signal of the *current* user; the service re-checks ownership on every
call (a foreign id is ``NotFoundError``, reported like a missing signal), so a forged callback
changes nothing and reveals nothing. A manual mark replaces an automatic result and the
pipeline never overwrites it afterwards. Cash-out and note ask for one typed line; their
state lives in ``ResultFlow`` and is refused as stale when the data is gone.
"""

from decimal import Decimal, InvalidOperation
from typing import Final

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot import texts
from app.bot.callbacks import ResultCb
from app.bot.draft import StaleDraftError
from app.bot.flow import FlowContext, FlowContextMiddleware
from app.bot.formatters import escape, format_signal_card
from app.bot.handlers.filters import TYPED_INPUT
from app.bot.keyboards import signal_keyboard
from app.bot.messaging import answer_callback, callback_message, edit_html, send_html
from app.bot.parsing import InputError, parse_number
from app.bot.result_buttons import MARK_CODES, ORIGIN_LIST
from app.bot.result_ui import format_pending, pending_keyboard
from app.bot.states import RUNNING_STATES, ResultFlow, leave_flow
from app.services.errors import NotFoundError, ValidationFailedError
from app.services.interfaces import (
    MAX_NOTE_LENGTH,
    ResultMark,
    ResultMarkRequest,
    SignalState,
    SignalView,
)

COMMAND: Final = "natijalar"
INPUT_KEY: Final = "result_input"
MAX_CASHOUT_UNITS: Final = Decimal(100)
"""A cash-out beyond this many units is a typo, not a result."""


def _message_of(callback: CallbackQuery) -> Message:
    message = callback_message(callback)
    if message is None:  # pragma: no cover - Telegram hides only very old messages
        raise StaleDraftError
    return message


# --------------------------------------------------------------------------- the list
async def _pending_screen(
    flow: FlowContext, page: int, notice: str | None = None
) -> tuple[str, InlineKeyboardMarkup | None]:
    signals = await flow.services.results.pending_results(flow.user_id)
    zone = await flow.zone()
    text = format_pending(signals, zone, page)
    return (f"{notice}\n\n{text}" if notice else text), pending_keyboard(signals, page)


async def show_pending(message: Message, flow: FlowContext) -> None:
    """``/natijalar``: the signals still without a result, one tap each."""
    await leave_flow(flow.state)
    text, markup = await _pending_screen(flow, 0)
    await send_html(message, text, markup)


async def on_list(callback: CallbackQuery, callback_data: ResultCb, flow: FlowContext) -> None:
    """◀️ ▶️ of the list (and a refresh)."""
    await answer_callback(callback)
    text, markup = await _pending_screen(flow, callback_data.page)
    await edit_html(_message_of(callback), text, markup)


# --------------------------------------------------------------------------- marking
async def _refusal(flow: FlowContext, signal_id: int) -> str:
    """Why the service refused the mark, in the user's words."""
    try:
        signal = await flow.services.signals.get_signal(flow.user_id, signal_id)
    except NotFoundError:
        return texts.RESULT_SIGNAL_GONE
    if signal.retro:
        return texts.RESULT_MARK_RETRO
    if signal.state is SignalState.CANCELLED:
        return texts.RESULT_MARK_CANCELLED
    return texts.RESULT_MARK_NOT_STARTED


async def _show_after(
    message: Message, flow: FlowContext, data: ResultCb, signal: SignalView, notice: str
) -> None:
    """Redraw what the button sat in: the list (origin ``l``) or the card."""
    if data.origin == ORIGIN_LIST:
        text, markup = await _pending_screen(flow, data.page, notice)
        await edit_html(message, text, markup)
        return
    zone = await flow.zone()
    card = f"{notice}\n\n{format_signal_card(signal, zone)}"
    await edit_html(message, card, signal_keyboard(signal, flow.clock()))


async def on_mark(callback: CallbackQuery, callback_data: ResultCb, flow: FlowContext) -> None:
    """✅ / ❌ / ↩️: set the signal's result by hand."""
    mark = MARK_CODES.get(callback_data.value)
    if mark is None:
        raise StaleDraftError
    request = ResultMarkRequest(signal_id=callback_data.signal_id, mark=mark)
    try:
        signal = await flow.services.results.mark_result(flow.user_id, request)
    except ValidationFailedError:
        await answer_callback(callback, await _refusal(flow, callback_data.signal_id), alert=True)
        return
    await answer_callback(callback)
    notice = texts.RESULT_MARKED.format(result=texts.RESULT_LABELS[mark])
    await _show_after(_message_of(callback), flow, callback_data, signal, notice)


# --------------------------------------------------------------------------- typed input
async def _ask(callback: CallbackQuery, data: ResultCb, flow: FlowContext, *, note: bool) -> None:
    """Remember what the next typed line is for and prompt for it."""
    if await flow.state.get_state() in RUNNING_STATES:
        await answer_callback(callback, texts.ALREADY_RUNNING, alert=True)
        return
    signal = await flow.services.signals.get_signal(flow.user_id, data.signal_id)
    if not note and not (signal.recommended and not signal.retro):
        await answer_callback(callback, await _refusal(flow, data.signal_id), alert=True)
        return
    await answer_callback(callback)
    await flow.state.update_data(
        {INPUT_KEY: {"signal_id": signal.signal_id, "page": data.page, "origin": data.origin}}
    )
    await flow.state.set_state(ResultFlow.note_input if note else ResultFlow.cashout_input)
    template = texts.RESULT_NOTE_PROMPT if note else texts.RESULT_CASHOUT_PROMPT
    prompt = template.format(fixture=escape(signal.fixture_label), limit=MAX_NOTE_LENGTH)
    await send_html(_message_of(callback), prompt)


async def on_cash(callback: CallbackQuery, callback_data: ResultCb, flow: FlowContext) -> None:
    """💸 Cashout: ask for the result in units."""
    await _ask(callback, callback_data, flow, note=False)


async def on_note(callback: CallbackQuery, callback_data: ResultCb, flow: FlowContext) -> None:
    """💬 Izoh: ask for the note."""
    await _ask(callback, callback_data, flow, note=True)


async def _pending_input(flow: FlowContext) -> ResultCb:
    """The stored target of the typed line, as the button that asked for it.

    Raises:
        StaleDraftError: nothing was asked (restart, ``/bekor`` and then a late message).
    """
    stored = (await flow.state.get_data()).get(INPUT_KEY)
    if not isinstance(stored, dict):
        await leave_flow(flow.state)
        raise StaleDraftError
    try:
        return ResultCb(
            action="input",
            signal_id=int(stored["signal_id"]),
            page=int(stored["page"]),
            origin=str(stored["origin"]),
        )
    except (KeyError, TypeError, ValueError):
        await leave_flow(flow.state)
        raise StaleDraftError from None


async def _finish_input(flow: FlowContext) -> None:
    data = await flow.state.get_data()
    data.pop(INPUT_KEY, None)
    await leave_flow(flow.state)
    await flow.state.set_data(data)


def parse_cashout_units(text: str) -> Decimal:
    """The typed cash-out result in units.

    Raises:
        InputError: not a number, or beyond any plausible result.
    """
    number = parse_number(text, example="0.8")
    try:
        value = Decimal(str(number))
    except InvalidOperation:  # pragma: no cover - parse_number already rejects non-finite
        raise InputError(texts.RESULT_CASHOUT_RANGE) from None
    if abs(value) > MAX_CASHOUT_UNITS:
        raise InputError(texts.RESULT_CASHOUT_RANGE)
    return value


async def on_cashout_text(message: Message, flow: FlowContext) -> None:
    """The typed cash-out result: mark the signal, then show what the button sat in."""
    target = await _pending_input(flow)
    units = parse_cashout_units(message.text or "")
    request = ResultMarkRequest(
        signal_id=target.signal_id, mark=ResultMark.CASHOUT, cashout_profit_units=units
    )
    try:
        signal = await flow.services.results.mark_result(flow.user_id, request)
    except ValidationFailedError:
        await _finish_input(flow)
        await send_html(message, await _refusal(flow, target.signal_id))
        return
    await _finish_input(flow)
    notice = texts.RESULT_MARKED.format(result=texts.RESULT_LABELS[ResultMark.CASHOUT])
    if target.origin == ORIGIN_LIST:
        text, markup = await _pending_screen(flow, target.page, notice)
        await send_html(message, text, markup)
        return
    zone = await flow.zone()
    await send_html(
        message,
        f"{notice}\n\n{format_signal_card(signal, zone)}",
        signal_keyboard(signal, flow.clock()),
    )


def clean_note(text: str) -> str:
    """The typed note as plain text (surrounding space trimmed).

    Raises:
        InputError: empty, or longer than :data:`MAX_NOTE_LENGTH`.
    """
    note = text.strip()
    if not note:
        raise InputError(texts.RESULT_NOTE_EMPTY)
    if len(note) > MAX_NOTE_LENGTH:
        raise InputError(texts.RESULT_NOTE_TOO_LONG.format(limit=MAX_NOTE_LENGTH, length=len(note)))
    return note


async def on_note_text(message: Message, flow: FlowContext) -> None:
    """The typed note: store it on the signal (it becomes context for later analyses)."""
    target = await _pending_input(flow)
    note = clean_note(message.text or "")
    try:
        await flow.services.results.add_note(flow.user_id, target.signal_id, note)
    except ValidationFailedError:
        raise InputError(texts.RESULT_NOTE_EMPTY) from None
    await _finish_input(flow)
    await send_html(message, texts.RESULT_NOTE_SAVED.format(note=escape(note)))


# --------------------------------------------------------------------------- router
def create_router() -> Router:
    """Build a fresh router (an aiogram router can be attached to only one dispatcher)."""
    router = Router(name="results")
    router.message.middleware(FlowContextMiddleware())
    router.callback_query.middleware(FlowContextMiddleware())
    router.message.register(show_pending, Command(COMMAND))
    router.message.register(on_cashout_text, TYPED_INPUT, StateFilter(ResultFlow.cashout_input))
    router.message.register(on_note_text, TYPED_INPUT, StateFilter(ResultFlow.note_input))
    for action, handler in (
        ("list", on_list),
        ("mark", on_mark),
        ("cash", on_cash),
        ("note", on_note),
    ):
        router.callback_query.register(handler, ResultCb.filter(F.action == action))
    return router
