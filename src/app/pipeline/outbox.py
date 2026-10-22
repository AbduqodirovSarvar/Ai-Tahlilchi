"""Best-effort delivery of secondary messages (watchlist events).

A watchlist event is an extra next to a primary message (an update or a result) that has
already been delivered, so its failure must never make the primary job fail or repeat.
:func:`deliver_watch_event` claims the event's key, sends it, and on a failure or an
unconfirmed delivery records the failed attempt (the key can be retried after its backoff).
"""

from app.logging import get_logger
from app.pipeline.context import PipelineContext
from app.pipeline.delivery import Delivery, WatchDelivery
from app.pipeline.errors import DeliveryNotConfirmedError
from app.pipeline.ledger import LedgerKey

logger = get_logger(__name__)


async def deliver_watch_event(
    ctx: PipelineContext, delivery: Delivery, user_id: int, message: WatchDelivery, key: LedgerKey
) -> bool:
    """Send ``message`` once under ``key``; returns whether it was delivered now."""
    if not await ctx.ledger.claim(key, now=ctx.clock()):
        return False
    try:
        reference = await delivery.watch_event(user_id, message, key)
        if reference is None:
            what = f"{message.event} event of fixture {message.fixture.id}"
            raise DeliveryNotConfirmedError(what)
    except Exception as error:  # best effort: the primary message already went out
        logger.warning("watch_event_failed", kind=message.event, error=repr(error))
        await ctx.ledger.fail(key, now=ctx.clock(), detail=str(error)[:200])
        return False
    await ctx.ledger.complete(key, now=ctx.clock())
    return True
