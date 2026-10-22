"""``/holat`` and the status/help menu item (plan §6.12, F6.9)."""

from collections.abc import Callable
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot import texts
from app.bot.account_formatters import format_status
from app.bot.messaging import send_html
from app.services.interfaces import Services

type Clock = Callable[[], datetime]


async def show_status(
    message: Message, services: Services, user_id: int, clock: Clock, backend: str = "fake"
) -> None:
    """API quota, data freshness, last job runs and errors, and which backend runs."""
    settings = await services.settings.get_settings(user_id)
    status = await services.status.system_status()
    await send_html(message, format_status(status, settings.zone, clock(), backend))


def create_router() -> Router:
    """Build a fresh router (an aiogram router can be attached to only one dispatcher)."""
    router = Router(name="status")
    router.message.register(show_status, Command("holat", "status"))
    router.message.register(show_status, F.text == texts.MENU_HELP)
    return router
