"""Last router: display-only buttons, stale buttons and text nobody else understood.

A button reaches this router when no handler accepts it in the current FSM state: the
bot restarted (in-memory FSM lost), the flow was cancelled or finished, or it belongs to
another step. The user is told to start again instead of seeing a spinning button.
"""

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import texts
from app.bot.callbacks import NoopCb
from app.bot.messaging import answer_callback, send_html
from app.logging import get_logger

logger = get_logger(__name__)


async def on_noop(callback: CallbackQuery) -> None:
    """Day headers, page indicators and empty calendar cells."""
    await answer_callback(callback)


async def on_stale_callback(callback: CallbackQuery) -> None:
    """Any button no handler accepted."""
    logger.info("stale_callback", data=callback.data)
    await answer_callback(callback, texts.STALE_BUTTON, alert=True)


async def on_unknown_text(message: Message, state: FSMContext) -> None:
    """Text outside any input step."""
    in_flow = await state.get_state() is not None
    await send_html(message, texts.USE_BUTTONS if in_flow else texts.UNKNOWN_TEXT)


def create_router() -> Router:
    """Build a fresh router (an aiogram router can be attached to only one dispatcher)."""
    router = Router(name="fallback")
    router.callback_query.register(on_noop, NoopCb.filter())
    router.callback_query.register(on_stale_callback)
    router.message.register(on_unknown_text)
    return router
