"""📋 Batafsil (plan §6.8, F7.2): the signal card turns into the fixture's detail card and back.

The card message is edited in place, so there is exactly one message per signal and the
button of the detail card always leads back to it.
"""

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot import keyboards
from app.bot.callbacks import CardCb
from app.bot.detail_formatters import format_fixture_detail
from app.bot.formatters import format_hidden_card, format_signal_card
from app.bot.messaging import answer_callback, callback_message, edit_html
from app.services.interfaces import Services, SignalState

ACTION_DETAIL = "detail"
ACTION_BACK = "card"


async def on_detail(
    callback: CallbackQuery, callback_data: CardCb, services: Services, user_id: int
) -> None:
    """📋 on a card: show the fixture's form, head to head, markets table and odds movement."""
    message = callback_message(callback)
    signal = await services.signals.get_signal(user_id, callback_data.signal_id)
    await answer_callback(callback)
    detail = await services.analysis.fixture_detail(signal.fixture_id)
    zone = (await services.settings.get_settings(user_id)).zone
    if message is not None:
        await edit_html(
            message,
            format_fixture_detail(detail, zone),
            keyboards.detail_keyboard(signal.signal_id),
        )


async def on_back_to_card(
    callback: CallbackQuery, callback_data: CardCb, services: Services, user_id: int
) -> None:
    """⬅️ on the detail card: the signal card again, with today's numbers."""
    message = callback_message(callback)
    signal = await services.signals.get_signal(user_id, callback_data.signal_id)
    await answer_callback(callback)
    zone = (await services.settings.get_settings(user_id)).zone
    if message is None:
        return
    if signal.state is SignalState.HIDDEN:
        await edit_html(message, format_hidden_card(signal))
        return
    await edit_html(message, format_signal_card(signal, zone), keyboards.signal_keyboard(signal))


def create_router() -> Router:
    """Build a fresh router (an aiogram router can be attached to only one dispatcher)."""
    router = Router(name="detail")
    router.callback_query.register(on_detail, CardCb.filter(F.action == ACTION_DETAIL))
    router.callback_query.register(on_back_to_card, CardCb.filter(F.action == ACTION_BACK))
    return router
