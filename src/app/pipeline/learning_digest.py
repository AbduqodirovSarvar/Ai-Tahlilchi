"""The weekly "what the model learned" message (F8.8): one per user per learning week.

Called by the scheduler right after the weekly learning review (not from inside it). The
content is read from the change journal, never from the review's return value, so the
announcement is repeatable: if the process died between the review and the messages, the
next call still finds the week's journal and the per-user keys decide who was already told.

Rules: nothing is sent while the loop is ``off``; users in their quiet hours are skipped (the
pipeline applies quiet hours, contract v1.2); a message that was not confirmed fails its key
(throttled administrator alert after repeated failures) and a later call retries it after the
backoff; one broken profile never stops the other users.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Final, Protocol, runtime_checkable

from app.db.repo import LearningChange, list_learning_changes, user_ids
from app.logging import get_logger
from app.pipeline.alerts import fail_key
from app.pipeline.context import PipelineContext
from app.pipeline.delivery import Delivery
from app.pipeline.errors import DeliveryNotConfirmedError
from app.pipeline.learning import week_start
from app.pipeline.learning_settings import LearningMode
from app.pipeline.ledger import LedgerKey
from app.pipeline.profile import load_profile

logger = get_logger(__name__)

JOB: Final = "learning_summary"
SUBJECT: Final = "weekly"
MAX_SUMMARY_CHANGES: Final = 100
"""Journal entries of one week read for the message (the loop writes a handful)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class LearningDelivery:
    """The week's journal as the pipeline sees it; the services layer turns it into DTOs."""

    week: date
    mode: LearningMode
    changes: tuple[LearningChange, ...]


@runtime_checkable
class SummaryDelivery(Protocol):
    """Optional port of a :class:`Delivery` that can carry the weekly summary."""

    async def learning_summary(
        self, user_id: int, message: LearningDelivery, key: LedgerKey
    ) -> str | None:
        """Send the weekly summary; ``None`` = not delivered (retry)."""
        ...


async def announce_learning(ctx: PipelineContext, delivery: Delivery) -> int:
    """Tell every user what the week's review changed; returns how many were told now."""
    mode = ctx.learning_settings.mode
    if mode is LearningMode.OFF or not isinstance(delivery, SummaryDelivery):
        return 0
    now = ctx.clock()
    week = week_start(now)
    since = datetime.combine(week, time.min, tzinfo=UTC)
    async with ctx.sessions() as session:
        changes = tuple(
            await list_learning_changes(session, limit=MAX_SUMMARY_CHANGES, since=since)
        )
        ids = await user_ids(session)
    message = LearningDelivery(week=week, mode=mode, changes=changes)
    told = 0
    for user_id in ids:
        told += await _announce_to(ctx, delivery, delivery, user_id, message)
    return told


async def _announce_to(
    ctx: PipelineContext,
    delivery: Delivery,
    carrier: SummaryDelivery,
    user_id: int,
    message: LearningDelivery,
) -> int:
    """One user's message: 1 if sent now, 0 if skipped or failed (never raises)."""
    now = ctx.clock()
    try:
        async with ctx.sessions() as session:
            profile = await load_profile(session, user_id)
        if profile.schedule.is_quiet(now):
            return 0
        key = LedgerKey(JOB, message.week, user_id, SUBJECT)
        if not await ctx.ledger.claim(key, now=now):
            return 0
    except Exception as error:  # a corrupt profile of one user must not stop the others
        logger.warning("learning_summary_profile_failed", user_id=user_id, error=repr(error))
        return 0
    try:
        reference = await carrier.learning_summary(user_id, message, key)
        if reference is None:
            raise DeliveryNotConfirmedError(f"learning summary of user {user_id}")
    except Exception as error:  # delivery failure is retried through the key's backoff
        logger.warning("learning_summary_failed", user_id=user_id, error=repr(error))
        await fail_key(ctx, delivery, key, error, what=f"learning summary of user {user_id}")
        return 0
    await ctx.ledger.complete(key, now=ctx.clock(), detail=reference)
    return 1
