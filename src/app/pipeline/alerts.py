"""Administrator alerts that cannot flood the chat.

A failing minute-tick would otherwise alert every minute. :func:`alert_throttled` sends at
most one alert per component in :data:`ALERT_THROTTLE` (the moment of the last alert is a
ledger stamp, so a restart does not reset it); the error itself is always recorded for
``/holat`` by the caller.
"""

from datetime import timedelta
from typing import Final

from app.logging import get_logger
from app.pipeline.context import PipelineContext
from app.pipeline.delivery import Delivery
from app.pipeline.ledger import Failure, LedgerKey
from app.pipeline.redaction import scrub_text

logger = get_logger(__name__)

ALERT_THROTTLE: Final = timedelta(minutes=30)
ALERT_JOB: Final = "alert"


async def alert_throttled(
    ctx: PipelineContext,
    delivery: Delivery,
    component: str,
    message: str,
    *,
    throttle_key: str | None = None,
) -> bool:
    """Alert the administrators about ``component`` unless one went out recently.

    Alerts share a throttle per ``throttle_key`` (default: the component), so "gave up for
    good" can use its own key and is never swallowed by the alert of an earlier, milder failure.

    Never raises: an alert that cannot be sent is logged, since the thing being reported is
    already a failure. Returns whether an alert was sent.
    """
    now = ctx.clock()
    throttle = throttle_key or component
    last = await ctx.ledger.stamped_at(ALERT_JOB, throttle)
    if last is not None and now - last < ALERT_THROTTLE:
        return False
    await ctx.ledger.stamp(ALERT_JOB, throttle, now=now)
    try:
        await delivery.alert_admin(component, scrub_text(message), now)
    except Exception as error:  # an unreachable admin chat must not hide the original error
        logger.warning("admin_alert_failed", component=component, error=repr(error))
        return False
    return True


async def fail_key(
    ctx: PipelineContext,
    delivery: Delivery,
    key: LedgerKey,
    error: Exception,
    *,
    what: str,
) -> Failure:
    """Record a failed attempt of ``key`` and alert the administrators (throttled, per ``key.job``).

    The ledger counts the attempt and sets the backoff; the error is kept for ``/holat``;
    after the last attempt ``what`` is reported as given up under its own throttle key, so that
    alert is never swallowed by the milder one of an earlier attempt.
    """
    job = key.job
    now = ctx.clock()
    failure = await ctx.ledger.fail(key, now=now, detail=str(error)[:200])
    await ctx.ledger.record_error(job, f"user {key.user_id}: {error}", now=now)
    state = "gave up" if failure.exhausted else "failed"
    await alert_throttled(
        ctx,
        delivery,
        job,
        f"{what} {state} (attempt {failure.attempts}): {error}",
        throttle_key=f"{job}.gave_up" if failure.exhausted else job,
    )
    return failure
