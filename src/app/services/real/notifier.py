"""Adapter from the pipeline's :class:`Delivery` to the contract's :class:`Notifier`.

The pipeline speaks storage-flavoured types; the bot-side ``Notifier`` speaks service DTOs.
This module converts one into the other and turns message references into the opaque
``"chat:message"`` strings the ledger keeps, so a later update can reply to the digest.
"""

from datetime import date, datetime

from app.logging import get_logger
from app.pipeline.delivery import (
    CancelDelivery,
    DailyDelivery,
    SettledDelivery,
    UpdateDelivery,
    WatchDelivery,
)
from app.pipeline.ledger import LedgerKey
from app.services.interfaces import (
    AdminAlert,
    AnalysisResult,
    CancelReason,
    DailyDigest,
    DateWindow,
    DeliveryKey,
    FixtureOption,
    MessageRef,
    Notifier,
    SettledNotice,
    SignalCancelled,
    SignalUpdate,
    UpdateKind,
    WatchlistEvent,
    WatchNotification,
)
from app.services.real.analysis import recalc_diff
from app.services.real.core import Core, to_view
from app.services.real.signals import RealResultService

logger = get_logger(__name__)


class LogOnlyNotifier:
    """A :class:`Notifier` that only logs: used until the bot provides a real one."""

    async def send_daily_digest(
        self, user_id: int, digest: DailyDigest, *, key: DeliveryKey
    ) -> MessageRef | None:
        """Log the digest."""
        logger.info("notify_daily", user_id=user_id, signals=len(digest.result.signals), key=key)
        return None

    async def send_signal_update(
        self,
        user_id: int,
        update: SignalUpdate,
        *,
        key: DeliveryKey,
        reply_to: MessageRef | None = None,
    ) -> MessageRef | None:
        """Log the update."""
        logger.info("notify_update", user_id=user_id, kind=update.kind, key=key, reply=reply_to)
        return None

    async def send_signal_cancelled(
        self,
        user_id: int,
        cancelled: SignalCancelled,
        *,
        key: DeliveryKey,
        reply_to: MessageRef | None = None,  # noqa: ARG002 - protocol signature
    ) -> MessageRef | None:
        """Log the cancellation."""
        logger.info("notify_cancelled", user_id=user_id, reason=cancelled.reason, key=key)
        return None

    async def send_watchlist_event(
        self, user_id: int, event: WatchlistEvent, *, key: DeliveryKey
    ) -> MessageRef | None:
        """Log the event."""
        logger.info("notify_watch", user_id=user_id, kind=event.kind, key=key)
        return None

    async def send_result_settled(
        self,
        user_id: int,
        notice: SettledNotice,  # noqa: ARG002 - protocol signature
        *,
        key: DeliveryKey,
    ) -> MessageRef | None:
        """Log the settlement."""
        logger.info("notify_settled", user_id=user_id, key=key)
        return None

    async def alert_admin(self, alert: AdminAlert) -> None:
        """Log the alert."""
        logger.error("admin_alert", component=alert.component, message=alert.message)


def _key(key: LedgerKey) -> DeliveryKey:
    return DeliveryKey(job=key.job, day=key.day, user_id=key.user_id, subject=key.subject)


def _encode(ref: MessageRef | None) -> str | None:
    return None if ref is None else f"{ref.chat_id}:{ref.message_id}"


def _decode(text: str | None) -> MessageRef | None:
    if not text or ":" not in text:
        return None
    chat, _, message = text.partition(":")
    return MessageRef(int(chat), int(message))


class NotifierDelivery:
    """The pipeline's :class:`~app.pipeline.delivery.Delivery` backed by a :class:`Notifier`."""

    def __init__(self, core: Core, notifier: Notifier) -> None:
        """Send through ``notifier``; views are built with ``core``."""
        self._core = core
        self._notifier = notifier

    async def daily(self, user_id: int, message: DailyDelivery, key: LedgerKey) -> str | None:
        """Send the morning digest, preceded by yesterday's tally when there is one."""
        core = self._core
        outcome = message.outcome
        result = AnalysisResult(
            query_id=f"daily:{message.day.isoformat()}",
            window=DateWindow(message.day, message.day),
            signals=tuple(to_view(core, c.record, card=c) for c in outcome.cards),
            fixtures_analyzed=outcome.fixtures_analyzed,
            matched=outcome.matched,
            requested=outcome.requested,
            api_requests_used=outcome.api_requests_used,
            failed_fixtures=outcome.failed_fixtures,
            skipped_no_price=outcome.skipped_no_price,
            skipped_stale_price=outcome.skipped_stale_price,
            data_stale=outcome.data_stale,
        )
        summary = await RealResultService(core).day_summary(user_id, _previous(message.day))
        digest = DailyDigest(
            day=message.day, result=result, yesterday=summary if summary.signals else None
        )
        return _encode(await self._notifier.send_daily_digest(user_id, digest, key=_key(key)))

    async def update(
        self, user_id: int, message: UpdateDelivery, key: LedgerKey, reply_to: str | None
    ) -> str | None:
        """Send a scheduled recalculation."""
        update = SignalUpdate(
            kind=UpdateKind(message.kind.value),
            diff=recalc_diff(self._core, message.result),
            lineups_confirmed=message.lineups_confirmed,
        )
        sent = await self._notifier.send_signal_update(
            user_id, update, key=_key(key), reply_to=_decode(reply_to)
        )
        return _encode(sent)

    async def cancelled(
        self, user_id: int, message: CancelDelivery, key: LedgerKey, reply_to: str | None
    ) -> str | None:
        """Send a cancellation."""
        cancelled = SignalCancelled(
            signal=to_view(self._core, message.card.record, card=message.card),
            reason=CancelReason(message.reason),
        )
        sent = await self._notifier.send_signal_cancelled(
            user_id, cancelled, key=_key(key), reply_to=_decode(reply_to)
        )
        return _encode(sent)

    async def watch_event(self, user_id: int, message: WatchDelivery, key: LedgerKey) -> str | None:
        """Send a watchlist event."""
        fixture = message.fixture.fixture
        event = WatchlistEvent(
            kind=WatchNotification(message.event),
            fixture=FixtureOption(
                fixture_id=str(message.fixture.id),
                sport=fixture.sport,
                league=fixture.league.name,
                home_team=fixture.home.name,
                away_team=fixture.away.name,
                kickoff=fixture.kickoff,
            ),
            detail=message.detail,
            signal=None
            if message.card is None
            else to_view(self._core, message.card.record, card=message.card),
        )
        return _encode(await self._notifier.send_watchlist_event(user_id, event, key=_key(key)))

    async def settled(self, user_id: int, message: SettledDelivery, key: LedgerKey) -> str | None:
        """Send a settlement."""
        notice = SettledNotice(
            signal=to_view(self._core, message.card.record, card=message.card),
            profit_units=message.profit_units,
        )
        return _encode(await self._notifier.send_result_settled(user_id, notice, key=_key(key)))

    async def alert_admin(self, component: str, message: str, at: datetime) -> None:
        """Report a system error to the administrators."""
        await self._notifier.alert_admin(AdminAlert(component=component, message=message, at=at))


def _previous(day: date) -> date:
    from datetime import timedelta  # noqa: PLC0415

    return day - timedelta(days=1)
