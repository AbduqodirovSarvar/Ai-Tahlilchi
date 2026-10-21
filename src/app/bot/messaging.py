"""Telegram I/O helpers: HTML sending with splitting, safe edits/answers and flood control.

Handlers use these instead of raw ``message.answer`` so every message gets
``parse_mode=HTML``, respects the 4096-character limit and survives the harmless
"message is not modified" / "query is too old" errors Telegram returns on double taps.
"""

import asyncio
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Final

from aiogram import Bot
from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import TelegramMethod
from aiogram.methods.base import Response, TelegramType
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
)

from app.bot.formatters import split_message
from app.logging import get_logger

logger = get_logger(__name__)

type Markup = InlineKeyboardMarkup | ReplyKeyboardMarkup
type Sleep = Callable[[float], Awaitable[None]]

MAX_RETRY_AFTER_SECONDS: Final = 60
"""Longer flood waits are not slept through: the error propagates (and is logged)."""
CALLBACK_ALERT_LIMIT: Final = 200
"""Telegram's maximum length of a callback answer text."""


class FloodControlMiddleware(BaseRequestMiddleware):
    """Retry a request once after Telegram's ``retry_after`` (429 flood control).

    Installed on the bot session, so it covers every API call (sends, edits, answers)
    without each handler having to care.
    """

    def __init__(self, sleep: Sleep = asyncio.sleep) -> None:
        """``sleep`` is injectable for tests."""
        self._sleep = sleep

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        """Send; on flood control wait ``retry_after`` seconds and send once more."""
        try:
            return await make_request(bot, method)
        except TelegramRetryAfter as error:
            if error.retry_after > MAX_RETRY_AFTER_SECONDS:
                raise
            logger.warning(
                "telegram_flood_control",
                method=type(method).__name__,
                retry_after=error.retry_after,
            )
            await self._sleep(float(error.retry_after))
            return await make_request(bot, method)


def install_flood_control(bot: Bot, sleep: Sleep = asyncio.sleep) -> None:
    """Register :class:`FloodControlMiddleware` on ``bot``'s session."""
    bot.session.middleware(FloodControlMiddleware(sleep))


async def send_html(message: Message, text: str, markup: Markup | None = None) -> Message:
    """Send ``text`` (HTML) to the chat of ``message``, split into ≤ 4096-char parts.

    The keyboard is attached to the last part; the last sent message is returned.
    """
    parts = split_message(text)
    sent = message
    for position, part in enumerate(parts):
        last = position == len(parts) - 1
        sent = await message.answer(
            part, parse_mode=ParseMode.HTML, reply_markup=markup if last else None
        )
    return sent


async def edit_html(
    message: Message, text: str, markup: InlineKeyboardMarkup | None = None
) -> None:
    """Edit a bot message in place; "message is not modified" (double tap) is ignored."""
    try:
        await message.edit_text(
            split_message(text)[0], parse_mode=ParseMode.HTML, reply_markup=markup
        )
    except TelegramBadRequest as error:
        if "not modified" not in error.message:
            raise


async def drop_keyboard(bot: Bot, chat_id: int, message_id: int | None) -> None:
    """Best-effort removal of an old wizard keyboard (it may be gone or unchanged)."""
    if message_id is None:
        return
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id)
    except TelegramAPIError as error:
        logger.debug("drop_keyboard_failed", error=error.message)


@dataclass(slots=True)
class AnswerTracker:
    """Whether the callback of the current update was already answered.

    Telegram accepts one answer per callback; once it is spent, a failure later in the
    handler has to reach the user as a message instead (see ``ErrorMiddleware``).
    """

    answered: bool = False


_tracker: ContextVar[AnswerTracker | None] = ContextVar("answer_tracker", default=None)


def track_answers() -> AnswerTracker:
    """Start tracking for the update handled in the current context."""
    tracker = AnswerTracker()
    _tracker.set(tracker)
    return tracker


async def answer_callback(
    callback: CallbackQuery, text: str | None = None, *, alert: bool = False
) -> None:
    """Answer a callback query, ignoring "query is too old"/"already answered" errors."""
    tracker = _tracker.get()
    if tracker is not None:
        tracker.answered = True
    if text is not None and len(text) > CALLBACK_ALERT_LIMIT:
        text = text[: CALLBACK_ALERT_LIMIT - 1] + "…"
    try:
        await callback.answer(text, show_alert=alert)
    except TelegramBadRequest as error:
        logger.debug("callback_answer_failed", error=error.message)


def callback_message(callback: CallbackQuery) -> Message | None:
    """The message a button belongs to, or ``None`` when Telegram no longer exposes it."""
    message = callback.message
    return message if isinstance(message, Message) else None
