"""Bot middleware: whitelist, user context, per-user rate limit and error → message mapping.

Order (see :func:`app.bot.dispatcher.build_dispatcher`): whitelist (outer, drops strangers
silently) → user context → rate limit → error mapping (inner, around every handler).
"""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

import structlog
from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Chat, Message, TelegramObject, Update, User

from app.bot import texts
from app.bot.draft import StaleDraftError
from app.bot.formatters import budget_message, cooldown_message, escape, validation_message
from app.bot.messaging import (
    CALLBACK_ALERT_LIMIT,
    AnswerTracker,
    answer_callback,
    callback_message,
    send_html,
    track_answers,
)
from app.bot.parsing import InputError
from app.logging import get_logger
from app.services.errors import (
    BudgetExceededError,
    BusyError,
    CooldownActiveError,
    NotFoundError,
    ServiceError,
    ServiceUnavailableError,
    ValidationFailedError,
)

logger = get_logger(__name__)

# aiogram's middleware protocol is typed with `dict[str, Any]` / `Any`; we mirror it exactly.
type Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]
type Monotonic = Callable[[], float]

RATE_LIMIT_CAPACITY: Final = 8
"""Burst of updates a user may send at once (a quick series of button taps)."""
RATE_LIMIT_REFILL_PER_SECOND: Final = 1.0


class WhitelistMiddleware(BaseMiddleware):
    """Drop every update that is not from a whitelisted user in a private chat with the bot.

    This is a personal bot: signals and stakes must never be posted where others can read
    them, so groups and channels are ignored even when an admin writes there. Updates without
    a chat (inline queries, callbacks on inline-mode messages) are ignored for the same reason.
    Rejected updates get no reply at all, so strangers cannot even confirm the bot is alive.
    Register it as an *outer* middleware on ``Dispatcher.update`` so it runs before any
    filter or handler (aiogram has already resolved ``event_from_user`` at that point).
    """

    def __init__(self, allowed_user_ids: frozenset[int]) -> None:
        """Store the whitelist; an empty set rejects everyone."""
        self._allowed_user_ids = allowed_user_ids

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        """Pass the update on only if it is a whitelisted user's private chat."""
        user: User | None = data.get("event_from_user")
        chat: Chat | None = data.get("event_chat")
        if not self._is_allowed(user, chat):
            logger.debug(
                "update_rejected",
                user_id=None if user is None else user.id,
                chat_type=None if chat is None else chat.type,
            )
            return None
        return await handler(event, data)

    def _is_allowed(self, user: User | None, chat: Chat | None) -> bool:
        return (
            user is not None
            and user.id in self._allowed_user_ids
            and chat is not None
            and chat.type == ChatType.PRIVATE
        )


class UserContextMiddleware(BaseMiddleware):
    """Expose ``user_id`` to handlers and bind it to every log line of the update."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        """Runs after the whitelist, so a user is always present."""
        user: User | None = data.get("event_from_user")
        if user is None:  # pragma: no cover - the whitelist already dropped such updates
            return await handler(event, data)
        data["user_id"] = user.id
        with structlog.contextvars.bound_contextvars(user_id=user.id):
            return await handler(event, data)


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float
    warned: bool = False


class RateLimitMiddleware(BaseMiddleware):
    """Per-user token bucket: a short burst is fine, sustained flooding is throttled.

    The first throttled update gets a friendly "slow down" reply; further ones are dropped
    silently until the bucket refills, so the limiter never becomes a spam source itself.
    """

    def __init__(
        self,
        capacity: int = RATE_LIMIT_CAPACITY,
        refill_per_second: float = RATE_LIMIT_REFILL_PER_SECOND,
        monotonic: Monotonic = time.monotonic,
    ) -> None:
        """``monotonic`` is injectable for tests."""
        self._capacity = float(capacity)
        self._refill = refill_per_second
        self._monotonic = monotonic
        self._buckets: dict[int, _Bucket] = {}

    def _take(self, user_id: int) -> _Bucket | None:
        """Consume a token; return the bucket when the user is over the limit."""
        now = self._monotonic()
        bucket = self._buckets.setdefault(user_id, _Bucket(self._capacity, now))
        bucket.tokens = min(
            self._capacity, bucket.tokens + (now - bucket.updated_at) * self._refill
        )
        bucket.updated_at = now
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            bucket.warned = False
            return None
        return bucket

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        """Call the handler unless the user is over the limit."""
        user: User | None = data.get("event_from_user")
        if user is None:  # pragma: no cover - the whitelist already dropped such updates
            return await handler(event, data)
        bucket = self._take(user.id)
        if bucket is None:
            return await handler(event, data)
        logger.info("rate_limited", user_id=user.id)
        inner = event.callback_query or event.message if isinstance(event, Update) else event
        if isinstance(inner, CallbackQuery):
            await answer_callback(inner, texts.RATE_LIMITED)
        elif isinstance(inner, Message) and not bucket.warned:
            await send_html(inner, texts.RATE_LIMITED)
        bucket.warned = True
        return None


def user_message_for(error: Exception) -> str:
    """The Uzbek message for an error a handler let escape."""
    message: str
    match error:
        case InputError():
            message = error.message
        case StaleDraftError():
            message = texts.STALE_BUTTON_HINT
        case CooldownActiveError():
            message = cooldown_message(error)
        case BudgetExceededError():
            message = budget_message(error)
        case BusyError():
            message = texts.BUSY
        case ServiceUnavailableError():
            message = texts.SERVICE_UNAVAILABLE.format(component=escape(error.component))
        case ValidationFailedError():
            message = validation_message(error)
        case NotFoundError():
            message = texts.NOT_FOUND
        case _:
            message = texts.UNEXPECTED_ERROR
    return message


class ErrorMiddleware(BaseMiddleware):
    """Turn handler failures into a helpful message instead of silence.

    Expected failures (bad input, service rules) are logged at INFO with their type;
    anything else is a bug: logged with traceback and answered with a generic apology.
    Callback queries are always answered, so the button never keeps spinning.
    """

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        """Run the handler and report its failure to the user."""
        tracker = data["answer_tracker"] = track_answers()
        try:
            return await handler(event, data)
        except (InputError, StaleDraftError, ServiceError) as error:
            logger.info("handler_rejected", error_type=type(error).__name__, detail=str(error))
            await self._report(event, user_message_for(error), tracker)
        except Exception:
            logger.exception("handler_failed", event_type=type(event).__name__)
            await self._report(event, texts.UNEXPECTED_ERROR, tracker)
        return None

    async def _report(self, event: TelegramObject, text: str, tracker: AnswerTracker) -> None:
        try:
            if isinstance(event, Message):
                await send_html(event, text)
            elif isinstance(event, CallbackQuery):
                # A handler may have answered before it failed (e.g. a toggle answers
                # "Saqlandi" and then edits the message); a second answer is rejected by
                # Telegram, so the error has to go out as a message.
                await self._report_callback(event, text, answered=tracker.answered)
        except TelegramAPIError:
            logger.warning("error_report_failed", exc_info=True)

    @staticmethod
    async def _report_callback(callback: CallbackQuery, text: str, *, answered: bool) -> None:
        message = callback_message(callback)
        if message is None or (len(text) <= CALLBACK_ALERT_LIMIT and not answered):
            await answer_callback(callback, _plain(text), alert=True)
            return
        if not answered:
            await answer_callback(callback)
        await send_html(message, text)


def _plain(text: str) -> str:
    """Alerts are plain text: strip the few HTML tags our messages use."""
    for tag in ("<b>", "</b>", "<code>", "</code>", "<i>", "</i>"):
        text = text.replace(tag, "")
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
