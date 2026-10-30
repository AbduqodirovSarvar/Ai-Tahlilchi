"""⚽ O'yinlar (``/oyinlar``): a day's matches, match sheets and model forecasts.

Matches and sheets come live from Football-Data.org; forecasts (📊) are the model's
probabilities from the stored history, without any odds.

Nothing here reads stored fixtures: every screen asks the service, which asks the provider.
Buttons carry only the day (a date ordinal), the page and the match id, so they are stateless
and survive a restart. A forged or out-of-range day is treated as today.
"""

from datetime import date
from typing import Final

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot import texts
from app.bot.callbacks import MatchCb
from app.bot.draft import StaleDraftError
from app.bot.expert_ui import format_expert_report
from app.bot.flow import FlowContext, FlowContextMiddleware
from app.bot.forecast_ui import (
    day_forecast_keyboard,
    format_day_forecast,
    format_match_forecast,
    match_forecast_keyboard,
)
from app.bot.handlers.filters import TYPED_INPUT
from app.bot.matches_ui import (
    clamp_page,
    format_match_sheet,
    format_matches,
    matches_keyboard,
    ordered_for_display,
    sheet_keyboard,
)
from app.bot.messaging import answer_callback, callback_message, edit_html, send_html
from app.bot.parsing import parse_date
from app.bot.picks_ui import format_top_picks, top_picks_keyboard
from app.bot.states import MatchFlow

COMMAND: Final = "oyinlar"
_FIRST_ORDINAL: Final = date.min.toordinal()
_LAST_ORDINAL: Final = date.max.toordinal()


def _message_of(callback: CallbackQuery) -> Message:
    message = callback_message(callback)
    if message is None:  # pragma: no cover - Telegram hides only very old messages
        raise StaleDraftError
    return message


async def _day_of(ordinal: int, flow: FlowContext) -> date:
    if _FIRST_ORDINAL <= ordinal <= _LAST_ORDINAL:
        return date.fromordinal(ordinal)
    return await flow.today()


async def _render_day(flow: FlowContext, day: date, page: int) -> tuple[str, InlineKeyboardMarkup]:
    items = ordered_for_display(await flow.services.matches.list_matches(flow.user_id, day, day))
    page = clamp_page(page, len(items))
    zone = await flow.zone()
    return (
        format_matches(day, items, page, zone),
        matches_keyboard(items, day, page, await flow.today()),
    )


async def open_matches(message: Message, flow: FlowContext) -> None:
    """⚽ O'yinlar / ``/oyinlar``: today's matches."""
    if await flow.state.get_state() == MatchFlow.date_query.state:
        await flow.state.set_state(None)
    text, markup = await _render_day(flow, await flow.today(), 0)
    await send_html(message, text, markup)


async def on_day(callback: CallbackQuery, callback_data: MatchCb, flow: FlowContext) -> None:
    """A day button (Bugun, Ertaga, neighbouring day) or a page of the list."""
    await answer_callback(callback)
    day = await _day_of(callback_data.d, flow)
    text, markup = await _render_day(flow, day, callback_data.p)
    await edit_html(_message_of(callback), text, markup)


async def on_ask(callback: CallbackQuery, flow: FlowContext) -> None:
    """🗓 Sana kiritish: wait for a typed date."""
    await answer_callback(callback)
    await flow.state.set_state(MatchFlow.date_query)
    await send_html(_message_of(callback), texts.MATCHES_ASK_DATE)


async def on_date_text(message: Message, flow: FlowContext) -> None:
    """The typed date: that day's matches (a bad date keeps waiting and says why)."""
    day = parse_date(message.text or "", await flow.today())
    await flow.state.set_state(None)
    text, markup = await _render_day(flow, day, 0)
    await send_html(message, text, markup)


async def on_item(callback: CallbackQuery, callback_data: MatchCb, flow: FlowContext) -> None:
    """A number button: the match sheet with coaches and line-ups."""
    await answer_callback(callback)
    sheet = await flow.services.matches.get_match(flow.user_id, callback_data.i)
    await edit_html(
        _message_of(callback),
        format_match_sheet(sheet, await flow.zone()),
        sheet_keyboard(callback_data.d, callback_data.p, callback_data.i),
    )


async def on_forecast_day(
    callback: CallbackQuery, callback_data: MatchCb, flow: FlowContext
) -> None:
    """📊 Kun prognozi: the model's view of every match of the day (paged)."""
    await answer_callback(callback)
    day = await _day_of(callback_data.d, flow)
    lines = await flow.services.matches.forecast_day(flow.user_id, day)
    page = clamp_page(callback_data.p, len(lines))
    await edit_html(
        _message_of(callback),
        format_day_forecast(day, lines, page, await flow.zone()),
        day_forecast_keyboard(day, page, len(lines)),
    )


async def on_forecast_match(
    callback: CallbackQuery, callback_data: MatchCb, flow: FlowContext
) -> None:
    """📊 Prognoz: every forecast market of one match with fair odds."""
    await answer_callback(callback)
    line = await flow.services.matches.forecast_match(flow.user_id, callback_data.i)
    await edit_html(
        _message_of(callback),
        format_match_forecast(line, await flow.zone()),
        match_forecast_keyboard(callback_data.d, callback_data.p, callback_data.i),
    )


async def on_picks(callback: CallbackQuery, callback_data: MatchCb, flow: FlowContext) -> None:
    """🎯 Eng ehtimolli tanlovlar of the day: the most likely picks, without odds."""
    await answer_callback(callback)
    day = await _day_of(callback_data.d, flow)
    picks = await flow.services.matches.top_picks(flow.user_id, day, day)
    settings = await flow.settings()
    await edit_html(
        _message_of(callback),
        format_top_picks(
            picks,
            day_from=day,
            day_to=day,
            min_probability=settings.min_probability,
            zone=settings.zone,
        ),
        top_picks_keyboard(day),
    )


async def on_expert(callback: CallbackQuery, callback_data: MatchCb, flow: FlowContext) -> None:
    """🧠 Ekspert tahlili of the day: 1-2 picks for every match, sent as new message(s)."""
    await answer_callback(callback)
    message = _message_of(callback)
    day = await _day_of(callback_data.d, flow)
    await edit_html(message, texts.EXPERT_PROGRESS)
    report = await flow.services.matches.expert_picks(flow.user_id, day, day)
    text = format_expert_report(report, day_from=day, day_to=day, zone=await flow.zone())
    await send_html(message, text, top_picks_keyboard(day))


def create_router() -> Router:
    """Build a fresh router (an aiogram router can be attached to only one dispatcher)."""
    router = Router(name="matches")
    router.message.middleware(FlowContextMiddleware())
    router.callback_query.middleware(FlowContextMiddleware())
    router.message.register(open_matches, F.text == texts.MENU_MATCHES)
    router.message.register(open_matches, Command(COMMAND))
    router.message.register(on_date_text, TYPED_INPUT, StateFilter(MatchFlow.date_query))
    for action, handler in (
        ("day", on_day),
        ("ask", on_ask),
        ("item", on_item),
        ("fday", on_forecast_day),
        ("fmatch", on_forecast_match),
        ("picks", on_picks),
        ("expert", on_expert),
    ):
        router.callback_query.register(handler, MatchCb.filter(F.action == action))
    return router
