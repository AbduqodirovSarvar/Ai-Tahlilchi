"""``/start``, ``/yordam``, ``/bekor`` and the main-menu buttons of later features.

Menu items whose features arrive in later waves (F7) answer with a polite
"tez orada" message but stay wired, so the menu layout is final from day one.
"""

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot import keyboards, texts
from app.bot.draft import StaleDraftError
from app.bot.messaging import drop_keyboard, send_html
from app.bot.states import drop_draft, load_draft


async def _leave_flows(message: Message, state: FSMContext) -> bool:
    """End any wizard/settings input; True when something was active."""
    active = await state.get_state() is not None
    try:
        draft = await load_draft(state)
    except StaleDraftError:
        draft = None
    if draft is not None and message.bot is not None:
        await drop_keyboard(message.bot, message.chat.id, draft.message_id)
    await drop_draft(state)
    return active or draft is not None


async def handle_start(message: Message, state: FSMContext) -> None:
    """Welcome text and the main menu; any unfinished flow is closed."""
    await _leave_flows(message, state)
    await send_html(message, texts.WELCOME, keyboards.main_menu())


async def handle_help(message: Message) -> None:
    """Commands and a short glossary (EV, CLV, unit)."""
    await send_html(message, texts.HELP, keyboards.main_menu())


async def handle_cancel(message: Message, state: FSMContext) -> None:
    """``/bekor``: leave the current flow."""
    was_active = await _leave_flows(message, state)
    await send_html(message, texts.CANCELLED if was_active else texts.NOTHING_TO_CANCEL)


def create_router() -> Router:
    """Build a fresh router (an aiogram router can be attached to only one dispatcher)."""
    router = Router(name="start")
    router.message.register(handle_start, CommandStart())
    router.message.register(handle_help, Command("yordam", "help"))
    router.message.register(handle_cancel, Command("bekor", "cancel"))
    return router
