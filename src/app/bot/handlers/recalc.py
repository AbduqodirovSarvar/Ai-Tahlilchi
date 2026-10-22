"""🔄 Qayta hisoblash (plan §6.4, F6.7).

* the card button recalculates one signal: the card is edited in place and the before/after
  diff follows as a reply to it;
* "🔄 Hammasini yangilash" (under an analysis summary or a digest) and ``/qayta`` recalculate
  many signals in a background job with a progress message, then post one summary.

Errors a recalculation can raise (cooldown, budget, busy, provider down) are plain
:class:`~app.services.errors.ServiceError` s: the error middleware or the job maps them to
Uzbek messages (:func:`app.bot.middleware.user_message_for`).
"""

from collections.abc import Awaitable, Callable
from datetime import date, datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards, texts
from app.bot.callbacks import CardCb, RecalcAllCb, decode_ref
from app.bot.formatters import escape, format_signal_card, local_today
from app.bot.handlers.analysis_run import ProgressReporter
from app.bot.jobs import BackgroundJobs
from app.bot.messaging import answer_callback, callback_message, edit_html, send_html
from app.bot.middleware import user_message_for
from app.bot.notify_formatters import format_recalc_batch, format_recalc_diff
from app.bot.parsing import InputError, parse_quick_query
from app.logging import get_logger
from app.services.errors import ServiceError
from app.services.interfaces import (
    MIN_EV_LOWER,
    MIN_EV_UPPER,
    MIN_PROBABILITY_LOWER,
    MIN_PROBABILITY_UPPER,
    DateSpec,
    ProgressCallback,
    RecalcBatch,
    Services,
)

logger = get_logger(__name__)

COMMAND = "qayta"

type BatchCall = Callable[[ProgressCallback], Awaitable[RecalcBatch]]
type Clock = Callable[[], datetime]


async def on_recalc_card(
    callback: CallbackQuery, callback_data: CardCb, services: Services, user_id: int
) -> None:
    """🔄 on a card: new numbers in the card, the diff as a reply under it."""
    message = callback_message(callback)
    await answer_callback(callback, texts.RECALC_STARTED)
    diff = await services.analysis.recalc_signal(user_id, callback_data.signal_id)
    if message is None:  # pragma: no cover - Telegram hides only very old messages
        return
    zone = (await services.settings.get_settings(user_id)).zone
    await edit_html(
        message, format_signal_card(diff.signal, zone), keyboards.signal_keyboard(diff.signal)
    )
    await send_html(message, format_recalc_diff(diff, zone))


async def on_recalc_all(
    callback: CallbackQuery,
    callback_data: RecalcAllCb,
    services: Services,
    user_id: int,
    jobs: BackgroundJobs,
) -> None:
    """🔄 Hammasini yangilash: recalculate the whole analysis or digest in the background."""
    message = callback_message(callback)
    call = _batch_call(services, user_id, callback_data)
    if call is None or message is None:
        await answer_callback(callback, texts.STALE_BUTTON, alert=True)
        return
    await answer_callback(callback, texts.RECALC_STARTED)
    await start_batch(message, services, user_id, jobs, call)


def _batch_call(services: Services, user_id: int, data: RecalcAllCb) -> BatchCall | None:
    """The service call a button names, or ``None`` when its reference is unusable."""
    if data.kind == keyboards.RECALC_KIND_QUERY:
        query_id = decode_ref(data.ref)
        if query_id is None:
            return None
        return lambda progress: services.analysis.recalc_query(user_id, query_id, progress=progress)
    if data.kind == keyboards.RECALC_KIND_DAY:
        try:
            day = date.fromisoformat(data.ref)
        except ValueError:
            return None
        return lambda progress: services.analysis.recalc_window(
            user_id, DateSpec.between(day, day), progress=progress
        )
    return None


async def start_batch(
    message: Message, services: Services, user_id: int, jobs: BackgroundJobs, call: BatchCall
) -> None:
    """Show the progress message and run ``call`` as a background job."""
    progress_message = await send_html(message, texts.RECALC_STARTED)
    jobs.spawn(
        _run_batch(message, progress_message, services, user_id, call),
        name=f"recalc:{user_id}",
    )


async def _run_batch(
    message: Message, progress_message: Message, services: Services, user_id: int, call: BatchCall
) -> None:
    """The job: recalculate, then post the summary, or explain the failure."""
    reporter = ProgressReporter(progress_message, stages=texts.RECALC_STAGES)
    try:
        batch = await call(reporter)
        zone = (await services.settings.get_settings(user_id)).zone
        summary = format_recalc_batch(batch, zone)
    except ServiceError as error:
        logger.info("recalc_rejected", error_type=type(error).__name__, detail=str(error))
        summary = user_message_for(error)
    except Exception:
        logger.exception("recalc_failed", user_id=user_id)
        summary = texts.UNEXPECTED_ERROR
    await _publish(message, progress_message, summary)


async def _publish(message: Message, progress_message: Message, summary: str) -> None:
    """Close the progress message and post the summary; an unreachable chat is only logged."""
    try:
        await edit_html(progress_message, texts.RECALC_DONE)
        await send_html(message, summary)
    except TelegramAPIError as error:
        logger.warning("recalc_publish_failed", error=error.message)


async def on_qayta(
    message: Message, services: Services, user_id: int, clock: Clock, jobs: BackgroundJobs
) -> None:
    """``/qayta [bugun|ertaga|dam|Nkun|sana oralig'i]``: recalculate the window's signals."""
    settings = await services.settings.get_settings(user_id)
    argument = (message.text or "").partition(" ")[2]
    spec = _window_spec(argument, local_today(clock(), settings.zone))
    await start_batch(
        message,
        services,
        user_id,
        jobs,
        lambda progress: services.analysis.recalc_window(user_id, spec, progress=progress),
    )


def _window_spec(argument: str | None, today: date) -> DateSpec:
    """The window of ``/qayta``: today without an argument, else one date expression.

    Raises:
        InputError: the argument is not just a date / range keyword.
    """
    text = (argument or "").strip()
    if not text:
        return DateSpec.today()
    try:
        quick = parse_quick_query(
            text,
            today,
            probability_bounds=(MIN_PROBABILITY_LOWER, MIN_PROBABILITY_UPPER),
            ev_bounds=(MIN_EV_LOWER, MIN_EV_UPPER),
        )
    except InputError as error:
        raise InputError(texts.QAYTA_BAD_ARGUMENT.format(problem=error.message)) from None
    if quick.dates is None or quick.team_names or quick.sports or quick.max_signals:
        raise InputError(
            texts.QAYTA_BAD_ARGUMENT.format(
                problem=texts.QAYTA_NOT_A_WINDOW.format(value=escape(text))
            )
        )
    return quick.dates


def create_router() -> Router:
    """Build a fresh router (an aiogram router can be attached to only one dispatcher)."""
    router = Router(name="recalc")
    router.callback_query.register(on_recalc_card, CardCb.filter(F.action == "recalc"))
    router.callback_query.register(on_recalc_all, RecalcAllCb.filter())
    router.message.register(on_qayta, Command(COMMAND))
    return router
