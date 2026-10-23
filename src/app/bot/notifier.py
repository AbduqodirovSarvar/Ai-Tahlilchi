"""Telegram implementation of the :class:`~app.services.interfaces.Notifier` port (F4 → bot).

The pipeline decides *whether* and *when* to notify (idempotency keys, quiet hours); this
class only renders and delivers. Delivery never raises: a blocked bot, a deleted chat, a
network failure or flood control that persists all end in ``None`` plus a log line, so a
scheduled job is never failed by one unreachable user.

Updates are sent as a *reply* to the earlier message instead of editing it: the earlier
message is usually a digest with many cards, and the update message must keep the
before/after numbers readable next to the original.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import Final
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import InlineKeyboardMarkup, ReplyParameters

from app.bot import keyboards
from app.bot.formatters import (
    PageItem,
    format_results_summary,
    format_signal_card,
    result_page,
    split_message,
)
from app.bot.learning_ui import format_summary
from app.bot.notify_formatters import (
    format_admin_alert,
    format_cancelled,
    format_digest_title,
    format_limit_digest,
    format_settled,
    format_update,
    format_watch_event,
)
from app.bot.result_buttons import card_result_rows
from app.logging import get_logger
from app.services.interfaces import (
    AdminAlert,
    DailyDigest,
    DeliveryKey,
    DigestProgress,
    DigestResume,
    LearningSummary,
    MessageRef,
    SettingsService,
    SettledNotice,
    SignalCancelled,
    SignalUpdate,
    WatchlistEvent,
)

logger = get_logger(__name__)

MAX_RETRY_AFTER_SECONDS: Final = 60
"""A longer flood wait is not slept through inside a scheduled job: the message is dropped."""
SEND_ATTEMPTS: Final = 2
"""One send plus one retry after Telegram's ``retry_after``."""
BLOCKED_TTL_SECONDS: Final = 24 * 60 * 60
"""A user who blocked the bot is skipped for this long, then tried again (they may have
unblocked it). Within the period the pipeline is told not to retry (contract v1.6)."""


class _Failure(Enum):
    """Why a message did not go out: ``REJECTED`` will never work, ``TRANSIENT`` may later."""

    NONE = auto()
    REJECTED = auto()
    TRANSIENT = auto()


@dataclass(frozen=True, slots=True)
class _Sent:
    """Outcome of one logical message: its first part's reference and how it failed, if it did."""

    ref: MessageRef | None
    failure: _Failure = _Failure.NONE


_TRANSIENT_ERRORS: Final = (TelegramNetworkError, TelegramServerError)
"""Telegram errors worth retrying later (a persistent 429 is handled separately)."""

type Sleep = Callable[[float], Awaitable[None]]
type Monotonic = Callable[[], float]


class TelegramNotifier:
    """Sends the pipeline's notifications to Telegram users.

    The bot is attached after construction (:meth:`bind`, or as an aiogram startup hook:
    :meth:`on_startup`) because the services need the notifier before the bot connects. Until
    then every call returns ``None`` and logs a warning.
    """

    def __init__(
        self,
        *,
        admin_ids: frozenset[int],
        default_zone: ZoneInfo,
        sleep: Sleep = asyncio.sleep,
        monotonic: Monotonic = time.monotonic,
    ) -> None:
        """``admin_ids`` receive alerts; ``default_zone`` is used when a user's is unknown."""
        self._admin_ids = admin_ids
        self._default_zone = default_zone
        self._sleep = sleep
        self._monotonic = monotonic
        self._blocked_until: dict[int, float] = {}
        self._bot: Bot | None = None
        self._settings: SettingsService | None = None

    def bind(self, bot: Bot) -> None:
        """Start delivering through ``bot``."""
        self._bot = bot

    async def on_startup(self, bot: Bot) -> None:
        """Startup hook of the dispatcher, which passes the connected bot."""
        self.bind(bot)

    def attach_settings(self, settings: SettingsService) -> None:
        """Format times in each user's own time zone."""
        self._settings = settings

    def is_undeliverable(self, user_id: int) -> bool:
        """True while ``user_id`` is known to have blocked the bot (contract v1.6).

        The pipeline then completes the delivery key instead of retrying it for days and
        alerting the admin about a user who simply left.
        """
        until = self._blocked_until.get(user_id)
        if until is None:
            return False
        if self._monotonic() >= until:
            del self._blocked_until[user_id]
            return False
        return True

    # ------------------------------------------------------------------ Notifier port
    async def send_daily_digest(
        self, user_id: int, digest: DailyDigest, *, key: DeliveryKey
    ) -> MessageRef | None:
        """Title, tally and shortfall in one message, then every card with its buttons."""
        return await self._guarded(key, self._daily(user_id, digest))

    async def send_signal_update(
        self,
        user_id: int,
        update: SignalUpdate,
        *,
        key: DeliveryKey,
        reply_to: MessageRef | None = None,
    ) -> MessageRef | None:
        """The 24h / pre-match diff, as a reply to the signal's earlier message."""
        return await self._guarded(key, self._update(user_id, update, reply_to))

    async def send_signal_cancelled(
        self,
        user_id: int,
        cancelled: SignalCancelled,
        *,
        key: DeliveryKey,
        reply_to: MessageRef | None = None,
    ) -> MessageRef | None:
        """Tell about the cancellation, as a reply when the earlier message is known."""
        return await self._guarded(key, self._cancelled(user_id, cancelled, reply_to))

    async def send_watchlist_event(
        self, user_id: int, event: WatchlistEvent, *, key: DeliveryKey
    ) -> MessageRef | None:
        """Line-ups / odds move / result of a watched team or league."""
        return await self._guarded(key, self._watch(user_id, event))

    async def send_result_settled(
        self, user_id: int, notice: SettledNotice, *, key: DeliveryKey
    ) -> MessageRef | None:
        """Announce the result (won / lost) with the profit in units."""
        return await self._guarded(key, self._settled(user_id, notice))

    async def send_learning_summary(
        self, user_id: int, summary: LearningSummary, *, key: DeliveryKey
    ) -> MessageRef | None:
        """The weekly "what the model learned" message."""
        return await self._guarded(key, self._learning(user_id, summary))

    async def alert_admin(self, alert: AdminAlert) -> None:
        """Tell every administrator; one unreachable admin does not stop the others."""
        for admin_id in sorted(self._admin_ids):
            zone = await self._zone(admin_id)
            try:
                await self._send(admin_id, format_admin_alert(alert, zone))
            except Exception:
                logger.exception("alert_admin_failed", admin_id=admin_id)

    # ------------------------------------------------------------------ message builders
    async def _daily(self, user_id: int, digest: DailyDigest) -> MessageRef | None:
        limit_text = format_limit_digest(digest)
        if limit_text is not None:  # one notice instead of an empty "0 signal" digest
            return await self._send(user_id, limit_text)
        zone = await self._zone(user_id)
        result = digest.result
        progress = digest.progress
        resume = await progress.resume() if progress else DigestResume()
        header = resume.header
        if header is None:
            text = f"{format_digest_title(digest)}\n{format_results_summary(result)}"
            markup = (
                keyboards.recalc_day_keyboard(digest.day)
                if result.signals and not result.retro
                else None
            )
            header = await self._send(user_id, text, markup)
            if header is None:
                return None
            if progress:
                await progress.header_sent(header)
        page = result_page(result.signals, zone, 0, max(len(result.signals), 1))
        complete = await self._send_cards(user_id, page, resume.handled, progress)
        return header if complete else None

    async def _send_cards(
        self,
        user_id: int,
        page: tuple[PageItem, ...],
        handled: frozenset[int],
        progress: DigestProgress | None,
    ) -> bool:
        """Send every message of ``page`` not yet handled; ``True`` when none needs a retry.

        A rejected card (400) is logged with its signal id and skipped, so it cannot hide the
        cards after it. A transient failure (a 429 that persists, network, 5xx) leaves that
        card undelivered but the loop goes on; the digest then counts as "not delivered" and
        the pipeline retries later, sending only the cards still missing (T6-F-1). A user who
        blocked the bot ends the loop at once (no retry).
        """
        complete = True
        for position, item in enumerate(page):
            if position in handled:
                continue
            markup = None if item.signal is None else keyboards.signal_keyboard(item.signal)
            sent = await self._send_detailed(user_id, item.text, markup)
            if sent.failure is _Failure.TRANSIENT:
                if self.is_undeliverable(user_id):
                    return True  # blocked: nothing more can be done, a retry would be futile
                complete = False
                continue
            if sent.failure is _Failure.REJECTED:
                logger.warning(
                    "notify_card_rejected",
                    user_id=user_id,
                    signal_id=None if item.signal is None else item.signal.signal_id,
                    position=position,
                )
            if progress:
                await progress.part_handled(position)
        return complete

    async def _update(
        self, user_id: int, update: SignalUpdate, reply_to: MessageRef | None
    ) -> MessageRef | None:
        zone = await self._zone(user_id)
        signal = update.diff.signal
        markup = keyboards.signal_keyboard(signal)
        return await self._send(user_id, format_update(update, zone), markup, reply_to)

    async def _cancelled(
        self, user_id: int, cancelled: SignalCancelled, reply_to: MessageRef | None
    ) -> MessageRef | None:
        return await self._send(user_id, format_cancelled(cancelled), reply_to=reply_to)

    async def _learning(self, user_id: int, summary: LearningSummary) -> MessageRef | None:
        zone = await self._zone(user_id)
        return await self._send(user_id, format_summary(summary, zone))

    async def _settled(self, user_id: int, notice: SettledNotice) -> MessageRef | None:
        rows = card_result_rows(notice.signal.signal_id)
        return await self._send(user_id, format_settled(notice), keyboards.markup_of(rows))

    async def _watch(self, user_id: int, event: WatchlistEvent) -> MessageRef | None:
        zone = await self._zone(user_id)
        text = format_watch_event(event, zone)
        markup = None
        if event.signal is not None:
            text = f"{text}\n\n{format_signal_card(event.signal, zone)}"
            markup = keyboards.signal_keyboard(event.signal)
        return await self._send(user_id, text, markup)

    # ------------------------------------------------------------------ delivery
    async def _guarded(
        self, key: DeliveryKey, work: Awaitable[MessageRef | None]
    ) -> MessageRef | None:
        """Run ``work``; a formatting or settings bug is logged and reported as undelivered."""
        try:
            ref = await work
        except Exception:
            logger.exception("notify_failed", job=key.job, user_id=key.user_id, subject=key.subject)
            return None
        logger.info(
            "notify_done",
            job=key.job,
            user_id=key.user_id,
            subject=key.subject,
            delivered=ref is not None,
        )
        return ref

    async def _zone(self, user_id: int) -> ZoneInfo:
        if self._settings is None:
            return self._default_zone
        try:
            return (await self._settings.get_settings(user_id)).zone
        except Exception:
            logger.warning("notify_zone_unavailable", user_id=user_id, exc_info=True)
            return self._default_zone

    async def _send(
        self,
        user_id: int,
        text: str,
        markup: InlineKeyboardMarkup | None = None,
        reply_to: MessageRef | None = None,
    ) -> MessageRef | None:
        """Send ``text`` in ≤ 4096-character parts; the reference is the first part's.

        ``None`` only when nothing was delivered. If a later part fails the earlier ones
        stay delivered and their reference is still returned.
        """
        return (await self._send_detailed(user_id, text, markup, reply_to)).ref

    async def _send_detailed(
        self,
        user_id: int,
        text: str,
        markup: InlineKeyboardMarkup | None = None,
        reply_to: MessageRef | None = None,
    ) -> _Sent:
        """Like :meth:`_send`, but says whether a failure is worth retrying later."""
        bot = self._bot
        if bot is None:
            logger.warning("notify_without_bot", user_id=user_id)
            return _Sent(None, _Failure.TRANSIENT)
        if self.is_undeliverable(user_id):
            logger.debug("notify_skipped_blocked_user", user_id=user_id)
            return _Sent(None, _Failure.TRANSIENT)
        parts = split_message(text)
        first: MessageRef | None = None
        for position, part in enumerate(parts):
            last = position == len(parts) - 1
            sent = await self._send_part(
                bot,
                user_id,
                part,
                markup=markup if last else None,
                reply_to=reply_to if position == 0 else None,
            )
            if sent.ref is None:
                return _Sent(first, sent.failure)
            first = first or sent.ref
        return _Sent(first)

    async def _send_part(
        self,
        bot: Bot,
        user_id: int,
        text: str,
        *,
        markup: InlineKeyboardMarkup | None,
        reply_to: MessageRef | None,
    ) -> _Sent:
        """One ``sendMessage`` with a single retry after flood control."""
        reply = (
            None
            if reply_to is None
            else ReplyParameters(message_id=reply_to.message_id, allow_sending_without_reply=True)
        )
        for attempt in range(1, SEND_ATTEMPTS + 1):
            try:
                message = await bot.send_message(
                    chat_id=user_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                    reply_parameters=reply,
                )
            except TelegramRetryAfter as error:
                if attempt == SEND_ATTEMPTS or error.retry_after > MAX_RETRY_AFTER_SECONDS:
                    logger.warning("notify_flood_control", user_id=user_id, wait=error.retry_after)
                    return _Sent(None, _Failure.TRANSIENT)
                await self._sleep(float(error.retry_after))
            except TelegramForbiddenError:
                logger.info("notify_user_blocked_bot", user_id=user_id)
                self._blocked_until[user_id] = self._monotonic() + BLOCKED_TTL_SECONDS
                return _Sent(None, _Failure.TRANSIENT)
            except TelegramAPIError as error:
                logger.warning("notify_rejected", user_id=user_id, error=error.message)
                transient = isinstance(error, _TRANSIENT_ERRORS)
                return _Sent(None, _Failure.TRANSIENT if transient else _Failure.REJECTED)
            else:
                return _Sent(MessageRef(message.chat.id, message.message_id))
        return _Sent(None, _Failure.TRANSIENT)  # pragma: no cover - the loop always returns
