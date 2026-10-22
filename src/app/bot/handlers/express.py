"""🧩 Ekspress tuzuvchi (plan §6.11, F7.8): pick 2-4 signals, see odds, probability and EV.

The picker is one message tied to ``ExpressDraft.message_id``; a button of an older picker, or
after a restart, is refused as stale. The numbers and the rules (different matches only) come
from ``ExpressService.evaluate``; a refusal from the service is explained, never swallowed.
"""

from typing import Final

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.types import CallbackQuery, Message

from app.bot import texts
from app.bot.callbacks import ExpressCb
from app.bot.draft import StaleDraftError
from app.bot.express_ui import (
    ExpressDraft,
    format_express,
    local_stamp,
    option_of,
    pick_keyboard,
    pick_text,
    pickable,
    result_keyboard,
)
from app.bot.flow import FlowContext, FlowContextMiddleware
from app.bot.messaging import answer_callback, callback_message, edit_html, send_html
from app.bot.states import ExpressFlow
from app.services.errors import ValidationFailedError
from app.services.interfaces import (
    MAX_EXPRESS_LEGS,
    MIN_EXPRESS_LEGS,
    DateSpec,
)

COMMAND: Final = "ekspress"
EXPRESS_KEY: Final = "express"
HORIZON_DAYS: Final = 3
"""Signals of today and the next two days are offered."""


async def _save(flow: FlowContext, draft: ExpressDraft) -> None:
    await flow.state.update_data({EXPRESS_KEY: draft.to_data()})
    await flow.state.set_state(ExpressFlow.pick)


async def _load(flow: FlowContext, callback: CallbackQuery) -> tuple[ExpressDraft, Message]:
    """The picker's draft and the message the button belongs to (an older one is stale)."""
    draft = ExpressDraft.from_data((await flow.state.get_data()).get(EXPRESS_KEY))
    message = callback_message(callback)
    if message is None or message.message_id != draft.message_id:
        raise StaleDraftError
    return draft, message


async def _drop(flow: FlowContext) -> None:
    data = await flow.state.get_data()
    data.pop(EXPRESS_KEY, None)
    await flow.state.set_state(None)
    await flow.state.set_data(data)


async def _options(flow: FlowContext) -> ExpressDraft:
    zone = await flow.zone()
    signals = await flow.services.signals.list_signals(
        flow.user_id, DateSpec.next_days(HORIZON_DAYS)
    )
    shown = pickable(signals, flow.clock())
    return ExpressDraft(options=tuple(option_of(s, local_stamp(s, zone)) for s in shown))


async def open_express(message: Message, flow: FlowContext) -> None:
    """``/ekspress`` or the button under today's signals: the picker."""
    draft = await _options(flow)
    if len(draft.options) < MIN_EXPRESS_LEGS:
        await send_html(message, texts.EXPRESS_EMPTY)
        return
    sent = await send_html(message, pick_text(draft), pick_keyboard(draft))
    await _save(flow, ExpressDraft(message_id=sent.message_id, options=draft.options))


async def on_open(callback: CallbackQuery, flow: FlowContext) -> None:
    """🧩 Ekspress under the today list."""
    message = callback_message(callback)
    await answer_callback(callback)
    if message is not None:
        await open_express(message, flow)


async def on_toggle(callback: CallbackQuery, callback_data: ExpressCb, flow: FlowContext) -> None:
    """Check or uncheck a signal; a second pick of one match or a fifth leg is refused."""
    draft, message = await _load(flow, callback)
    refusal = draft.refusal(callback_data.index)
    if refusal is not None:
        await answer_callback(callback, refusal, alert=True)
        return
    changed = draft.toggled(callback_data.index)
    await answer_callback(callback)
    await edit_html(message, pick_text(changed), pick_keyboard(changed))
    await _save(flow, changed)


async def on_calc(callback: CallbackQuery, flow: FlowContext) -> None:
    """🧮 Hisoblash: the combined numbers from the service."""
    draft, message = await _load(flow, callback)
    ids = [option.signal_id for option in draft.selected_options()]
    if not MIN_EXPRESS_LEGS <= len(ids) <= MAX_EXPRESS_LEGS:
        await answer_callback(
            callback, texts.EXPRESS_NEED_TWO.format(min=MIN_EXPRESS_LEGS), alert=True
        )
        return
    try:
        view = await flow.services.express.evaluate(flow.user_id, ids)
    except ValidationFailedError:
        await answer_callback(callback, texts.EXPRESS_REFUSED, alert=True)
        return
    await answer_callback(callback)
    await edit_html(message, format_express(view), result_keyboard())


async def on_back(callback: CallbackQuery, flow: FlowContext) -> None:
    """⬅️ Tanlashga qaytish: the picker with the same choice."""
    draft, message = await _load(flow, callback)
    await answer_callback(callback)
    await edit_html(message, pick_text(draft), pick_keyboard(draft))


async def on_close(callback: CallbackQuery, flow: FlowContext) -> None:
    """❌ Yopish."""
    _, message = await _load(flow, callback)
    await answer_callback(callback)
    await _drop(flow)
    await edit_html(message, texts.CANCELLED)


def create_router() -> Router:
    """Build a fresh router (an aiogram router can be attached to only one dispatcher)."""
    router = Router(name="express")
    router.message.middleware(FlowContextMiddleware())
    router.callback_query.middleware(FlowContextMiddleware())
    router.message.register(open_express, Command(COMMAND))
    router.callback_query.register(on_open, ExpressCb.filter(F.action == "open"))
    picking = StateFilter(ExpressFlow.pick)
    for action, handler in (
        ("toggle", on_toggle),
        ("calc", on_calc),
        ("back", on_back),
        ("close", on_close),
    ):
        router.callback_query.register(handler, ExpressCb.filter(F.action == action), picking)
    return router
