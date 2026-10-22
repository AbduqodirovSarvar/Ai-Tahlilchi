"""The daily digest (F4.3): each user's analysis at their own ``send_time``.

:func:`run_due_digests` is called every minute by the scheduler and decides per user:

- not yet their send time today (their time zone), or the digest is older than
  ``daily_catch_up`` (the process was down all morning) -> nothing;
- inside quiet hours -> held back, sent when they end (as long as the catch-up window lasts);
- otherwise claim ``(daily, local day, user, digest)`` *before* any network call, analyse,
  deliver, and mark done. A crash leaves a claim that expires with its lease; a failed analysis
  releases the key for the next tick; a finished key is never run again, so a rerun can neither
  duplicate signals nor messages.

The analysis is the same engine as every other path; only the request differs.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from app.logging import get_logger
from app.pipeline.analyze import AnalysisEngine
from app.pipeline.context import PipelineContext
from app.pipeline.delivery import DailyDelivery, Delivery
from app.pipeline.ledger import STAMP_DAY, LedgerKey
from app.pipeline.profile import UserProfile, default_request, load_profile
from app.pipeline.queries import user_ids

logger = get_logger(__name__)

JOB: Final = "daily"
SUBJECT: Final = "digest"
MESSAGE_JOB: Final = "msg"


def message_key(user_id: int, signal_id: int) -> LedgerKey:
    """Where the reference of the message that announced a signal is remembered."""
    return LedgerKey(MESSAGE_JOB, STAMP_DAY, user_id, f"signal:{signal_id}")


@dataclass(frozen=True, slots=True)
class DigestReport:
    """What one tick did: users served, held back (quiet hours) and failed."""

    sent: tuple[int, ...] = ()
    held: tuple[int, ...] = ()
    failed: tuple[int, ...] = ()


def send_moment(profile: UserProfile, now: datetime) -> datetime:
    """The UTC instant of today's ``send_time`` in the user's time zone."""
    local_day = now.astimezone(profile.zone).date()
    return datetime.combine(local_day, profile.settings.send_time, tzinfo=profile.zone).astimezone(
        now.tzinfo
    )


async def run_due_digests(ctx: PipelineContext, delivery: Delivery) -> DigestReport:
    """Send every digest that is due now (see the module docstring)."""
    async with ctx.sessions() as session:
        ids = await user_ids(session)
        profiles = [await load_profile(session, user_id) for user_id in ids]
    sent: list[int] = []
    held: list[int] = []
    failed: list[int] = []
    for profile in profiles:
        outcome = await _digest_for(ctx, delivery, profile)
        {"sent": sent, "held": held, "failed": failed}.get(outcome, []).append(profile.user_id)
    return DigestReport(tuple(sent), tuple(held), tuple(failed))


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
        await ctx.ledger.fail(key, now=ctx.clock(), detail=str(error)[:200])
        await ctx.ledger.record_error(JOB, f"user {profile.user_id}: {error}", now=ctx.clock())
        return "failed"
    return "sent"


async def _send(
    ctx: PipelineContext, delivery: Delivery, profile: UserProfile, key: LedgerKey
) -> None:
    day = key.day
    outcome = await AnalysisEngine(ctx).run(default_request(profile, day, day))
    reference = await delivery.daily(profile.user_id, DailyDelivery(day=day, outcome=outcome), key)
    now = ctx.clock()
    if reference is not None:
        for card in outcome.cards:
            if card.record.recommended_at is not None:
                await ctx.ledger.complete(
                    message_key(profile.user_id, card.record.id), now=now, detail=reference
                )
    await ctx.ledger.complete(key, now=now, detail=f"signals={len(outcome.cards)}")
    await ctx.ledger.record_run(JOB, ok=True, detail=f"user={profile.user_id}", now=now)


def next_send_after(profile: UserProfile, now: datetime) -> datetime:
    """The next UTC instant this user's digest becomes due (for status displays)."""
    due = send_moment(profile, now)
    return due if due > now else due + timedelta(days=1)
