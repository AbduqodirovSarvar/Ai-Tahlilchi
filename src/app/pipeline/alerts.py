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
        await delivery.alert_admin(component, message, now)
    except Exception as error:  # an unreachable admin chat must not hide the original error
        logger.warning("admin_alert_failed", component=component, error=repr(error))
        return False
    return True
