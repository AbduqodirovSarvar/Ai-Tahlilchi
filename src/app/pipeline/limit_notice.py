"""The "loss limit reached" notice: at most one per user and local day.

When a loss limit stops the new signals, the digest or template that would have carried them
sends this notice instead. Several deliveries can be blocked on the same day (the morning
digest and every scheduled template), but the user is told once: the first one sends the notice
and completes ``(limit, local day, user, notice)``, the others find it done and stay silent.
"""

from typing import Final

from app.pipeline.context import PipelineContext
from app.pipeline.delivery import DailyDelivery, Delivery
from app.pipeline.errors import DeliveryNotConfirmedError
from app.pipeline.ledger import LedgerKey
from app.pipeline.limits import LossStatus
from app.pipeline.profile import UserProfile
from app.pipeline.types import AnalysisOutcome

NOTICE_JOB: Final = "limit"
NOTICE_SUBJECT: Final = "notice"
LIMIT_DETAIL: Final = "loss_limit"
"""The ledger detail of a delivery that ended as (or behind) a limit notice."""
NO_SIGNALS: Final = AnalysisOutcome(
    cards=(), fixtures_analyzed=0, matched=0, requested=None, api_requests_used=0
)


async def announce_loss_limit(
    ctx: PipelineContext,
    delivery: Delivery,
    profile: UserProfile,
    status: LossStatus,
    key: LedgerKey,
) -> bool:
    """Tell the user about the reached limit unless they were told today; ``True`` if sent.

    ``key`` is the caller's own (claimed) idempotency key, passed on to the delivery so the
    message dedupes like any other. A notice that is not confirmed raises
    :class:`DeliveryNotConfirmedError`, so the caller's key fails and backs off as usual.
    """
    day = profile.schedule.local_day(ctx.clock())
    notice_key = LedgerKey(NOTICE_JOB, day, profile.user_id, NOTICE_SUBJECT)
    if await ctx.ledger.is_done(notice_key):
        return False
    message = DailyDelivery(day=day, outcome=NO_SIGNALS, loss_limit=status)
    reference = await delivery.daily(profile.user_id, message, key)
    if reference is None:
        raise DeliveryNotConfirmedError(f"loss limit notice of user {profile.user_id}")
    await ctx.ledger.complete(notice_key, now=ctx.clock(), detail=reference)
    return True
