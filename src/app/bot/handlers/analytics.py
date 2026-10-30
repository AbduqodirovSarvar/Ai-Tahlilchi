"""The analytics bot (v1.16): today's / tomorrow's matches, top leagues and a match analysis.

Lists come live from Football-Data.org through the matches service. A match button opens the
deep analysis of that match with only two buttons under it: 🔄 Yangilash and back to the list.
Buttons carry only the day ordinal or league code, the page and the match id, so they are
stateless and survive a restart. A forged or out-of-range day is treated as today.
"""

from datetime import date, timedelta
from typing import Final

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot import texts
from app.bot.analytics_ui import (
    LEAGUE_DAYS,
    analysis_keyboard,
    clamp_page,
    day_keyboard,
    format_analysis,
    format_day,
    format_league,
    league_keyboard,
    leagues_keyboard,
    leagues_text,
)
from app.bot.callbacks import GameCb
from app.bot.draft import StaleDraftError
from app.bot.flow import FlowContext, FlowContextMiddleware
from app.bot.keyboards import main_menu
from app.bot.messaging import answer_callback, callback_message, edit_html, send_html
from app.services.interfaces import MatchItem

_FIRST_ORDINAL: Final = date.min.toordinal()
_LAST_ORDINAL: Final = date.max.toordinal()
_KNOWN_LEAGUES: Final = frozenset(code for code, _label in texts.TOP_LEAGUES)


def _message_of(callback: CallbackQuery) -> Message:
    message = callback_message(callback)
    if message is None:  # pragma: no cover - Telegram hides only very old messages
        raise StaleDraftError
    return message


async def _day_of(ordinal: int, flow: FlowContext) -> date:
    if _FIRST_ORDINAL <= ordinal <= _LAST_ORDINAL:
        return date.fromordinal(ordinal)
    return await flow.today()


def _ordered(items: tuple[MatchItem, ...]) -> list[MatchItem]:
    return sorted(items, key=lambda item: (item.kickoff, item.competition, item.match_id))


async def _render_day(flow: FlowContext, day: date, page: int) -> tuple[str, InlineKeyboardMarkup]:
    items = _ordered(await flow.services.matches.list_matches(flow.user_id, day, day))
    page = clamp_page(page, len(items))
    return format_day(day, len(items)), day_keyboard(items, day, page, await flow.zone())


async def _render_league(
    flow: FlowContext, code: str, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    today = await flow.today()
    window = await flow.services.matches.list_matches(
        flow.user_id, today, today + timedelta(days=LEAGUE_DAYS - 1)
    )
    items = _ordered(tuple(item for item in window if item.competition_code == code))
    page = clamp_page(page, len(items))
    return format_league(code, len(items)), league_keyboard(items, code, page, await flow.zone())


async def handle_about(message: Message) -> None:
    """The help button (Yordam / Bot haqida) and ``/yordam``."""
    await send_html(message, texts.ABOUT, main_menu())


async def open_today(message: Message, flow: FlowContext) -> None:
    """📅 Bugungi o'yinlar."""
    text, markup = await _render_day(flow, await flow.today(), 0)
    await send_html(message, text, markup)


async def open_tomorrow(message: Message, flow: FlowContext) -> None:
    """🗓 Ertangi o'yinlar."""
    text, markup = await _render_day(flow, await flow.today() + timedelta(days=1), 0)
    await send_html(message, text, markup)


async def open_leagues(message: Message) -> None:
    """🏆 Top Ligalar tahlili: the league menu."""
    await send_html(message, leagues_text(), leagues_keyboard())


async def on_day(callback: CallbackQuery, callback_data: GameCb, flow: FlowContext) -> None:
    """A page of a day's list, or back to it from an analysis."""
    await answer_callback(callback)
    text, markup = await _render_day(flow, await _day_of(callback_data.d, flow), callback_data.p)
    await edit_html(_message_of(callback), text, markup)


async def on_leagues(callback: CallbackQuery) -> None:
    """Back to the league menu."""
    await answer_callback(callback)
    await edit_html(_message_of(callback), leagues_text(), leagues_keyboard())


async def on_league(callback: CallbackQuery, callback_data: GameCb, flow: FlowContext) -> None:
    """A league's matches of the next days (an unknown code shows the league menu)."""
    await answer_callback(callback)
    if callback_data.c not in _KNOWN_LEAGUES:
        await edit_html(_message_of(callback), leagues_text(), leagues_keyboard())
        return
    text, markup = await _render_league(flow, callback_data.c, callback_data.p)
    await edit_html(_message_of(callback), text, markup)


async def _show_analysis(
    callback: CallbackQuery, callback_data: GameCb, flow: FlowContext, *, refresh: bool
) -> None:
    await answer_callback(callback)
    message = _message_of(callback)
    await edit_html(message, texts.ANALYSIS_PROGRESS)
    analysis = await flow.services.matches.analyse_match(
        flow.user_id, callback_data.i, refresh=refresh
    )
    await edit_html(
        message, format_analysis(analysis, await flow.zone()), analysis_keyboard(callback_data)
    )


async def on_analyse(callback: CallbackQuery, callback_data: GameCb, flow: FlowContext) -> None:
    """A match button: its deep analysis."""
    await _show_analysis(callback, callback_data, flow, refresh=False)


async def on_refresh(callback: CallbackQuery, callback_data: GameCb, flow: FlowContext) -> None:
    """🔄 Yangilash: a fresh analysis (line-ups may have been announced since)."""
    await _show_analysis(callback, callback_data, flow, refresh=True)


def create_router() -> Router:
    """Build a fresh router (an aiogram router can be attached to only one dispatcher)."""
    router = Router(name="analytics")
    router.message.middleware(FlowContextMiddleware())
    router.callback_query.middleware(FlowContextMiddleware())
    router.message.register(open_today, F.text == texts.MENU_GAMES_TODAY)
    router.message.register(open_tomorrow, F.text == texts.MENU_GAMES_TOMORROW)
    router.message.register(open_leagues, F.text == texts.MENU_TOP_LEAGUES)
    router.message.register(handle_about, F.text == texts.MENU_ABOUT)
    router.message.register(handle_about, Command("yordam", "help"))
    router.message.register(open_today, Command("oyinlar"))
    for action, handler in (
        ("day", on_day),
        ("leagues", on_leagues),
        ("league", on_league),
        ("an", on_analyse),
        ("re", on_refresh),
    ):
        router.callback_query.register(handler, GameCb.filter(F.action == action))
    return router
