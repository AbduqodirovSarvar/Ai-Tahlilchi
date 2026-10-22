"""✅ Stavka qildim and 🎟 Mening stavkalarim: the minimal bets wiring the cards need.

The full bank UI (history chart, settling, loss-limit checks before betting) is F7.5; here a
user can record a bet from a card and list the open ones.

Idempotency (contract v1.2 ``BetDraft.client_token``): the token names the *prompt message*
and the signal. A double tap of "✅ Yozish" is two distinct callback queries (different ids)
for the same prompt, so the callback id itself cannot deduplicate it; the prompt message id
can. A typed bet uses the id of the user's message, which Telegram never reuses.
"""

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards, texts
from app.bot.account_formatters import (
    default_stake,
    format_bet_placed,
    format_bet_prompt,
    format_bets,
)
from app.bot.callbacks import BetCb, CardCb
from app.bot.formatters import odds
from app.bot.handlers.filters import TYPED_INPUT
from app.bot.messaging import (
    answer_callback,
    callback_message,
    drop_keyboard,
    edit_html,
    send_html,
)
from app.bot.parsing import parse_bet_input
from app.bot.states import (
    BetFlow,
    PendingBet,
    drop_bet_input,
    load_bet_input,
    start_bet_input,
)
from app.services.errors import ValidationFailedError
from app.services.interfaces import BetDraft, BetView, Services

DEDUP_PREFIX = "bet"
LIST_LIMIT = 20


def prompt_token(signal_id: int, message_id: int) -> str:
    """Idempotency token of a bet confirmed on prompt message ``message_id``."""
    return f"{DEDUP_PREFIX}:{signal_id}:{message_id}"


async def on_bet_card(
    callback: CallbackQuery, callback_data: CardCb, services: Services, user_id: int
) -> None:
    """✅ on a card: show the signal's odds and stake and ask to confirm or change them."""
    message = callback_message(callback)
    signal = await services.signals.get_signal(user_id, callback_data.signal_id)
    if signal.retro or message is None:
        raise ValidationFailedError("signal_id", "a retro signal cannot be bet on")
    settings = await services.settings.get_settings(user_id)
    await answer_callback(callback)
    await send_html(
        message,
        format_bet_prompt(signal, settings),
        keyboards.bet_prompt_keyboard(
            signal.signal_id,
            odds(signal.odds),
            can_use_defaults=default_stake(settings, signal) is not None,
        ),
    )


async def on_bet_prompt(
    callback: CallbackQuery,
    callback_data: BetCb,
    state: FSMContext,
    services: Services,
    user_id: int,
) -> None:
    """The prompt's buttons: record with the signal's numbers, type others, or cancel."""
    message = callback_message(callback)
    if message is None:  # pragma: no cover - Telegram hides only very old messages
        await answer_callback(callback, texts.STALE_BUTTON, alert=True)
        return
    match callback_data.action:
        case "ok":
            await answer_callback(callback)
            draft = BetDraft(
                signal_id=callback_data.signal_id,
                client_token=prompt_token(callback_data.signal_id, message.message_id),
            )
            await edit_html(message, await _record(services, user_id, draft))
        case "custom":
            await _ask_typed_values(callback, callback_data, state, message)
        case _:
            await answer_callback(callback)
            if await state.get_state() == BetFlow.value_input.state:
                await drop_bet_input(state)
            await edit_html(message, texts.BET_CANCELLED)


async def _ask_typed_values(
    callback: CallbackQuery, callback_data: BetCb, state: FSMContext, message: Message
) -> None:
    """Switch to typed input unless another flow (a wizard, a running analysis) is active."""
    if await state.get_state() is not None:
        await answer_callback(callback, texts.BET_FINISH_FLOW_FIRST, alert=True)
        return
    await answer_callback(callback)
    await start_bet_input(state, PendingBet(callback_data.signal_id, message.message_id))
    await edit_html(
        message, texts.BET_INPUT_PROMPT, keyboards.bet_cancel_keyboard(callback_data.signal_id)
    )


async def on_bet_text(
    message: Message, state: FSMContext, services: Services, user_id: int
) -> None:
    """The typed odds (and stake) of a bet."""
    pending = await load_bet_input(state)
    taken, stake = parse_bet_input(message.text or "")
    draft = BetDraft(
        signal_id=pending.signal_id,
        odds_taken=taken,
        stake=stake,
        client_token=prompt_token(pending.signal_id, message.message_id),
    )
    text = await _record(services, user_id, draft)
    await drop_bet_input(state)
    if message.bot is not None:
        await drop_keyboard(message.bot, message.chat.id, pending.message_id)
    await send_html(message, text)


async def _record(services: Services, user_id: int, draft: BetDraft) -> str:
    """Place the bet and build the confirmation (with the loss-limit warning when due)."""
    bet = await services.bets.place_bet(user_id, draft)
    bank = await services.bets.bank_summary(user_id)
    text = format_bet_placed(bet)
    return f"{text}\n\n{texts.BET_LOSS_LIMIT}" if bank.loss_limit_reached else text


async def show_bets(message: Message, services: Services, user_id: int) -> None:
    """🎟 Mening stavkalarim: bank snapshot and the open bets."""
    settings = await services.settings.get_settings(user_id)
    bank = await services.bets.bank_summary(user_id)
    bets: tuple[BetView, ...] = await services.bets.list_bets(
        user_id, open_only=True, limit=LIST_LIMIT
    )
    await send_html(message, format_bets(bank, bets, settings.zone))


def create_router() -> Router:
    """Build a fresh router (an aiogram router can be attached to only one dispatcher)."""
    router = Router(name="bets")
    router.message.register(show_bets, F.text == texts.MENU_BETS)
    router.message.register(on_bet_text, TYPED_INPUT, StateFilter(BetFlow.value_input))
    router.callback_query.register(on_bet_card, CardCb.filter(F.action == "bet"))
    router.callback_query.register(on_bet_prompt, BetCb.filter())
    return router
