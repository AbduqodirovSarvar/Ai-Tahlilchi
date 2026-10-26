"""📡 So'rovlar (``/sorovlar``): every call made to API-Football and Gemini, for administrators.

Buttons carry only the filter, the page and the call id (an integer), so every screen is
stateless and survives a restart. The service refuses non-administrators with
``NotAuthorizedError``, which the error middleware turns into a clear message.
"""

from typing import Final

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message

from app.bot import texts
from app.bot.callbacks import CallsCb
from app.bot.calls_ui import (
    calls_keyboard,
    detail_keyboard,
    format_call_file,
    format_calls,
    format_detail,
    format_stats,
    request_for,
    stats_keyboard,
)
from app.bot.draft import StaleDraftError
from app.bot.flow import FlowContext, FlowContextMiddleware
from app.bot.messaging import answer_callback, callback_message, edit_html, send_html
from app.bot.states import BrowseFlow
from app.services.interfaces import CallLogPage

COMMAND: Final = "sorovlar"
FILE_ENCODING: Final = "utf-8"


def _message_of(callback: CallbackQuery) -> Message:
    message = callback_message(callback)
    if message is None:  # pragma: no cover - Telegram hides only very old messages
        raise StaleDraftError
    return message


async def _stats_screen(flow: FlowContext) -> tuple[str, InlineKeyboardMarkup]:
    stats = await flow.services.calls.call_stats(flow.user_id, days=1)
    return format_stats(stats), stats_keyboard()


async def _page(flow: FlowContext, filter_code: str, number: int) -> CallLogPage:
    """The page asked for; one past the end (the log shrank) falls back to the last page."""
    log = flow.services.calls
    page = await log.list_calls(flow.user_id, request_for(filter_code, number))
    if not page.items and page.total and page.page_count:
        page = await log.list_calls(flow.user_id, request_for(filter_code, page.page_count - 1))
    return page


async def open_calls(message: Message, flow: FlowContext) -> None:
    """📡 So'rovlar / ``/sorovlar``: today's statistics and the filter buttons."""
    if await flow.state.get_state() == BrowseFlow.team_query.state:
        await flow.state.set_state(None)
    text, markup = await _stats_screen(flow)
    await send_html(message, text, markup)


async def on_stats(callback: CallbackQuery, flow: FlowContext) -> None:
    """📊 Statistika: back to the statistics screen."""
    await answer_callback(callback)
    text, markup = await _stats_screen(flow)
    await edit_html(_message_of(callback), text, markup)


async def on_list(callback: CallbackQuery, callback_data: CallsCb, flow: FlowContext) -> None:
    """A filter button or a page of the list."""
    await answer_callback(callback)
    page = await _page(flow, callback_data.f, callback_data.p)
    zone = await flow.zone()
    await edit_html(
        _message_of(callback),
        format_calls(page, callback_data.f, zone),
        calls_keyboard(page, callback_data.f),
    )


async def on_item(callback: CallbackQuery, callback_data: CallsCb, flow: FlowContext) -> None:
    """One call in full: header, request and response; the keyboard sits under the last part."""
    await answer_callback(callback)
    detail = await flow.services.calls.get_call(flow.user_id, callback_data.i)
    parts = format_detail(detail, await flow.zone())
    markup = detail_keyboard(detail, callback_data.f, callback_data.p)
    message = _message_of(callback)
    last = len(parts) - 1
    await edit_html(message, parts[0], markup if last == 0 else None)
    for position, part in enumerate(parts[1:], start=1):
        await send_html(message, part, markup if position == last else None)


async def on_file(callback: CallbackQuery, callback_data: CallsCb, flow: FlowContext) -> None:
    """📄 To'liq matnni fayl sifatida: the whole request and response as a ``.txt`` document."""
    await answer_callback(callback, texts.CALLS_FILE_SENDING)
    detail = await flow.services.calls.get_call(flow.user_id, callback_data.i)
    content = format_call_file(detail, await flow.zone()).encode(FILE_ENCODING)
    await _message_of(callback).answer_document(
        BufferedInputFile(content, filename=f"call_{detail.call_id}.txt"),
        caption=texts.CALLS_FILE_CAPTION.format(id=detail.call_id),
    )


def create_router() -> Router:
    """Build a fresh router (an aiogram router can be attached to only one dispatcher)."""
    router = Router(name="calls")
    router.message.middleware(FlowContextMiddleware())
    router.callback_query.middleware(FlowContextMiddleware())
    router.message.register(open_calls, F.text == texts.MENU_CALLS)
    router.message.register(open_calls, Command(COMMAND))
    for action, handler in (
        ("stats", on_stats),
        ("list", on_list),
        ("item", on_item),
        ("file", on_file),
    ):
        router.callback_query.register(handler, CallsCb.filter(F.action == action))
    return router
