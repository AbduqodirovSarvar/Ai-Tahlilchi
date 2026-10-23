"""``/ozgarishlar`` (plan 6.13 C, F8.8): the learning journal and undoing an entry.

The journal is system-wide (one model for every user). Undoing asks first, then calls the
pipeline's guarded rollback through ``LearningService``: an entry whose effect has been
superseded is refused (``STALE``) with an explanation instead of overwriting newer state, and a
rollback freezes the change for the manual-hold period, which the message states.
"""

from typing import Final

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.bot.callbacks import LearnCb
from app.bot.draft import StaleDraftError
from app.bot.flow import FlowContext, FlowContextMiddleware
from app.bot.learning_ui import (
    back_keyboard,
    format_journal,
    format_undo_ask,
    journal_keyboard,
    undo_keyboard,
    undo_outcome_text,
)
from app.bot.messaging import answer_callback, callback_message, edit_html, send_html
from app.bot.states import leave_flow
from app.services.interfaces import LearningChangeView, LearningPage, RollbackOutcome

COMMAND: Final = "ozgarishlar"


def _message_of(callback: CallbackQuery) -> Message:
    message = callback_message(callback)
    if message is None:  # pragma: no cover - Telegram hides only very old messages
        raise StaleDraftError
    return message


async def _page(flow: FlowContext, number: int) -> LearningPage:
    return await flow.services.learning.list_changes(flow.user_id, page=number)


async def show_journal(message: Message, flow: FlowContext) -> None:
    """``/ozgarishlar``: the newest entries first."""
    await leave_flow(flow.state)
    page = await _page(flow, 0)
    zone = await flow.zone()
    await send_html(message, format_journal(page, zone), journal_keyboard(page))


async def on_list(callback: CallbackQuery, callback_data: LearnCb, flow: FlowContext) -> None:
    """◀️ ▶️ of the journal (and ⬅️ back from a confirmation)."""
    await answer_callback(callback)
    page = await _page(flow, callback_data.page)
    zone = await flow.zone()
    await edit_html(_message_of(callback), format_journal(page, zone), journal_keyboard(page))


async def _entry(flow: FlowContext, change_id: int, page: int) -> tuple[LearningChangeView, int]:
    """The entry a button names, looked up again, and the hold days; stale when it is gone."""
    listing = await _page(flow, page)
    for change in listing.items:
        if change.change_id == change_id:
            return change, listing.hold_days
    raise StaleDraftError


async def on_ask(callback: CallbackQuery, callback_data: LearnCb, flow: FlowContext) -> None:
    """↩️ Qaytarish #N: confirm first."""
    change, hold_days = await _entry(flow, callback_data.id, callback_data.page)
    zone = await flow.zone()
    if not change.undoable:
        await answer_callback(callback, undo_outcome_text(_why_not(change), hold_days), alert=True)
        return
    await answer_callback(callback)
    await edit_html(
        _message_of(callback),
        format_undo_ask(change, zone, hold_days),
        undo_keyboard(change.change_id, callback_data.page),
    )


def _why_not(change: LearningChangeView) -> RollbackOutcome:
    """The answer for a button of an entry that cannot be undone (any more)."""
    if change.rolled_back:
        return RollbackOutcome.ALREADY_ROLLED_BACK
    return RollbackOutcome.NOT_UNDOABLE


async def on_undo(callback: CallbackQuery, callback_data: LearnCb, flow: FlowContext) -> None:
    """✅ Ha, qaytarish: undo through the pipeline's guarded rollback and say what happened."""
    outcome = await flow.services.learning.rollback_change(flow.user_id, callback_data.id)
    listing = await _page(flow, callback_data.page)
    await answer_callback(callback)
    await edit_html(
        _message_of(callback),
        undo_outcome_text(outcome, listing.hold_days),
        back_keyboard(listing.page),
    )


def create_router() -> Router:
    """Build a fresh router (an aiogram router can be attached to only one dispatcher)."""
    router = Router(name="learning")
    router.message.middleware(FlowContextMiddleware())
    router.callback_query.middleware(FlowContextMiddleware())
    router.message.register(show_journal, Command(COMMAND))
    for action, handler in (("list", on_list), ("ask", on_ask), ("undo", on_undo)):
        router.callback_query.register(handler, LearnCb.filter(F.action == action))
    return router
