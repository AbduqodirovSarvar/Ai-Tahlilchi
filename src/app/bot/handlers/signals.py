"""Signal card buttons and "➡️ Yana N ta" result pagination.

🙈 Yashirish is implemented here; 🔄 Qayta hisoblash lives in ``recalc``, ✅ Stavka qildim
in ``bets``. 📋 Batafsil (F7.2) is wired and answers with a "coming soon" alert.
"""

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from app.bot import texts
from app.bot.callbacks import CardCb, ResultsCb
from app.bot.formatters import format_hidden_card
from app.bot.handlers.analysis_run import send_result_page
from app.bot.messaging import answer_callback, callback_message, edit_html
from app.bot.states import load_results, save_results
from app.logging import get_logger
from app.services.interfaces import Services, SignalView

logger = get_logger(__name__)


async def on_card(
    callback: CallbackQuery, callback_data: CardCb, services: Services, user_id: int
) -> None:
    """Card buttons handled here: "🙈 Yashirish" (📋 Batafsil is still a placeholder)."""
    if callback_data.action != "hide":
        await answer_callback(callback, texts.PLACEHOLDER_ALERT, alert=True)
        return
    signal = await services.signals.hide_signal(user_id, callback_data.signal_id)
    await answer_callback(callback, texts.HIDDEN_ALERT)
    message = callback_message(callback)
    if message is not None:
        await edit_html(message, format_hidden_card(signal))


async def _fetch(services: Services, user_id: int, signal_ids: tuple[int, ...]) -> list[SignalView]:
    """Signals still available, in the stored order (the batch call skips vanished ones)."""
    signals = await services.signals.get_signals(user_id, signal_ids)
    missing = len(signal_ids) - len(signals)
    if missing:
        logger.info("result_signals_missing", count=missing)
    return list(signals)


async def on_results_page(
    callback: CallbackQuery,
    callback_data: ResultsCb,
    state: FSMContext,
    services: Services,
    user_id: int,
) -> None:
    """Send the next page of the last analysis result.

    The button names its result (``token``) and the page it continues. A button of an
    older result is refused; a repeated tap (the page was already served) is ignored.
    """
    stored = await load_results(state)
    if callback_data.token != stored.token:
        await answer_callback(callback, texts.STALE_BUTTON, alert=True)
        return
    message = callback_message(callback)
    await answer_callback(callback)
    if message is None:  # pragma: no cover - Telegram hides only very old messages
        return
    if callback_data.page != stored.next_page:
        return
    # The button is used once: remove it so a repeated tap cannot ask for the page again.
    await edit_html(message, message.html_text)
    settings = await services.settings.get_settings(user_id)
    signals = await _fetch(services, user_id, stored.signal_ids)
    await send_result_page(message, signals, settings.zone, callback_data.page, stored.token)
    await save_results(state, stored.advanced())


def create_router() -> Router:
    """Build a fresh router (an aiogram router can be attached to only one dispatcher)."""
    router = Router(name="signals")
    router.callback_query.register(on_card, CardCb.filter())
    router.callback_query.register(on_results_page, ResultsCb.filter())
    return router
