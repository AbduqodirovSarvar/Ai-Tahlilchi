"""⚡ Bugungi signallar (plan §6.1, F6.8): today's recommendations from the service.

The list is paged like an analysis result: the ids are remembered in FSM data and the
"➡️ Yana" button (handled in :mod:`app.bot.handlers.signals`) fetches the next page in one
batch call.
"""

from collections.abc import Callable
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot import keyboards, texts
from app.bot.formatters import local_today, ordered_for_display
from app.bot.handlers.analysis_run import send_result_page
from app.bot.messaging import send_html
from app.bot.states import next_results
from app.services.interfaces import DateSpec, Services

COMMAND = "bugun"

type Clock = Callable[[], datetime]


async def show_today(
    message: Message, state: FSMContext, services: Services, user_id: int, clock: Clock
) -> None:
    """Send today's signals (or the empty state) and remember them for paging."""
    settings = await services.settings.get_settings(user_id)
    signals = await services.signals.list_signals(user_id, DateSpec.today())
    if not signals:
        await send_html(message, texts.TODAY_EMPTY)
        return
    ordered = ordered_for_display(signals, settings.zone)
    stored = await next_results(state, tuple(signal.signal_id for signal in ordered))
    day = local_today(clock(), settings.zone)
    await send_html(
        message,
        texts.TODAY_HEADER.format(count=len(ordered)),
        keyboards.recalc_day_keyboard(day),
    )
    await send_result_page(message, ordered, settings.zone, 0, stored.token)


def create_router() -> Router:
    """Build a fresh router (an aiogram router can be attached to only one dispatcher)."""
    router = Router(name="today")
    router.message.register(show_today, F.text == texts.MENU_TODAY)
    router.message.register(show_today, Command(COMMAND))
    return router
