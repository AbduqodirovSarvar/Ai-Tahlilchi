"""The daily digest (F4.3): each user's analysis at their own ``send_time``.

:func:`run_due_digests` is called every minute by the scheduler and decides per user:

- not yet their send time today (their time zone), or the digest is older than
  ``daily_catch_up`` (the process was down all morning) -> nothing;
- inside quiet hours -> held back, sent when they end (as long as the catch-up window lasts);
- otherwise claim ``(daily, local day, user, digest)`` *before* any network call, analyse,
  deliver, and mark done. A crash leaves a claim that expires with its lease; a finished key is
  never run again, so a rerun can neither duplicate signals nor messages.

Failure: delivery is at least once, deduplicated by the key. The analysis result is stored
(the signal ids and counters, key ``daily.outcome``) before delivery is tried, so a retry
re-sends the *same* digest instead of re-running the analysis and the LLM; the retry waits
for an exponential backoff, a notifier answer of ``None`` ("not delivered": the bot is not
attached yet, the user is unreachable) counts as a failure, and after
:data:`app.pipeline.ledger.MAX_ATTEMPTS` tries the digest is given up on with a throttled
administrator alert. The analysis itself is the same engine as every other path.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from app.logging import get_logger
from app.pipeline.alerts import alert_throttled, fail_key
from app.pipeline.analyze import AnalysisEngine
from app.pipeline.context import PipelineContext
from app.pipeline.delivery import DailyDelivery, Delivery
from app.pipeline.digest_store import outcome_for, remember_announcement
from app.pipeline.errors import DeliveryNotConfirmedError
from app.pipeline.ledger import LedgerKey
from app.pipeline.limit_notice import LIMIT_DETAIL, announce_loss_limit
from app.pipeline.limits import load_loss_status
from app.pipeline.profile import UserProfile, default_request, load_profile, local_moment
from app.pipeline.queries import user_ids

logger = get_logger(__name__)

JOB: Final = "daily"
SUBJECT: Final = "digest"


@dataclass(frozen=True, slots=True)
class DigestReport:
    """What one tick did: users served, held back (quiet hours) and failed."""

    sent: tuple[int, ...] = ()
    held: tuple[int, ...] = ()
    failed: tuple[int, ...] = ()


def send_moment(profile: UserProfile, now: datetime) -> datetime:
    """The UTC instant of today's ``send_time`` in the user's time zone."""
    local_day = now.astimezone(profile.zone).date()
    return local_moment(profile.zone, local_day, profile.settings.send_time)


async def run_due_digests(ctx: PipelineContext, delivery: Delivery) -> DigestReport:
    """Send every digest that is due now (see the module docstring)."""
    async with ctx.sessions() as session:
        ids = await user_ids(session)
    sent: list[int] = []
    held: list[int] = []
    failed: list[int] = []
    for user_id in ids:
        outcome = await _serve(ctx, delivery, user_id)
        {"sent": sent, "held": held, "failed": failed}.get(outcome, []).append(user_id)
    return DigestReport(tuple(sent), tuple(held), tuple(failed))


async def _serve(ctx: PipelineContext, delivery: Delivery, user_id: int) -> str:
    """One user's digest; a broken profile or digest never stops the other users."""
    try:
        async with ctx.sessions() as session:
            profile = await load_profile(session, user_id)
    except Exception as error:  # e.g. a corrupt settings row of one user
        logger.warning("daily_profile_failed", user_id=user_id, error=repr(error))
        await ctx.ledger.record_error(JOB, f"user {user_id} profile: {error}", now=ctx.clock())
        await alert_throttled(ctx, delivery, JOB, f"user {user_id} profile: {error}")
        return "failed"
    return await _digest_for(ctx, delivery, profile)


async def _digest_for(ctx: PipelineContext, delivery: Delivery, profile: UserProfile) -> str:
    """``"sent"``, ``"held"``, ``"failed"`` or ``""`` (nothing due)."""
    now = ctx.clock()
    due = send_moment(profile, now)
    if now < due or now - due > ctx.config.daily_catch_up:
        return ""
    schedule = profile.schedule
    if schedule.is_quiet(now):
        return "held"
    day = schedule.local_day(now)
    key = LedgerKey(JOB, day, profile.user_id, SUBJECT)
    if not await ctx.ledger.claim(key, now=now):
        return ""
    try:
        await _send(ctx, delivery, profile, key)
    except Exception as error:  # a failure for one user must not stop the others
        logger.warning("daily_failed", user_id=profile.user_id, error=repr(error))
        await fail_key(ctx, delivery, key, error, what=f"digest of user {profile.user_id}")
        return "failed"
    return "sent"


async def _send(
    ctx: PipelineContext, delivery: Delivery, profile: UserProfile, key: LedgerKey
) -> None:
    status = await load_loss_status(ctx, profile, ctx.clock())
    if status.reached:  # responsible gaming: no new signals today, one notice instead
        await announce_loss_limit(ctx, delivery, profile, status, key)
        await _finish(ctx, profile, key, LIMIT_DETAIL)
        return
    day = key.day
    outcome = await outcome_for(
        ctx, key, lambda: AnalysisEngine(ctx).run(default_request(profile, day, day))
    )
    reference = await delivery.daily(profile.user_id, DailyDelivery(day=day, outcome=outcome), key)
    if reference is None:
        raise DeliveryNotConfirmedError(f"daily digest of user {profile.user_id}")
    await remember_announcement(ctx, profile.user_id, outcome, reference)
    await _finish(ctx, profile, key, f"signals={len(outcome.cards)}")


async def _finish(ctx: PipelineContext, profile: UserProfile, key: LedgerKey, detail: str) -> None:
    """Close the digest key and record the run for ``/holat``."""
    now = ctx.clock()
    await ctx.ledger.complete(key, now=now, detail=detail)
    await ctx.ledger.record_run(JOB, ok=True, detail=f"user={profile.user_id}", now=now)


def next_send_after(profile: UserProfile, now: datetime) -> datetime:
    """The next UTC instant this user's digest becomes due (for status displays)."""
    due = send_moment(profile, now)
    return due if due > now else due + timedelta(days=1)
