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

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from app.db.repo import get_signal
from app.logging import get_logger
from app.pipeline.alerts import alert_throttled
from app.pipeline.analyze import AnalysisEngine
from app.pipeline.context import PipelineContext
from app.pipeline.delivery import DailyDelivery, Delivery
from app.pipeline.errors import DeliveryNotConfirmedError
from app.pipeline.ledger import STAMP_DAY, LedgerKey
from app.pipeline.profile import UserProfile, default_request, load_profile
from app.pipeline.queries import user_ids
from app.pipeline.types import AnalysisOutcome, SignalCard

logger = get_logger(__name__)

JOB: Final = "daily"
SUBJECT: Final = "digest"
MESSAGE_JOB: Final = "msg"
OUTCOME_JOB: Final = "daily.outcome"


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
        failure = await ctx.ledger.fail(key, now=ctx.clock(), detail=str(error)[:200])
        await ctx.ledger.record_error(JOB, f"user {profile.user_id}: {error}", now=ctx.clock())
        state = "gave up" if failure.exhausted else "failed"
        message = f"digest of user {profile.user_id} {state} (attempt {failure.attempts}): {error}"
        await alert_throttled(
            ctx, delivery, JOB, message, throttle_key=f"{JOB}.gave_up" if failure.exhausted else JOB
        )
        return "failed"
    return "sent"


async def _send(
    ctx: PipelineContext, delivery: Delivery, profile: UserProfile, key: LedgerKey
) -> None:
    day = key.day
    outcome = await _outcome(ctx, profile, key)
    reference = await delivery.daily(profile.user_id, DailyDelivery(day=day, outcome=outcome), key)
    if reference is None:
        raise DeliveryNotConfirmedError(f"daily digest of user {profile.user_id}")
    now = ctx.clock()
    for card in outcome.cards:
        if card.record.recommended_at is not None:
            await ctx.ledger.complete(
                message_key(profile.user_id, card.record.id), now=now, detail=reference
            )
    await ctx.ledger.complete(key, now=now, detail=f"signals={len(outcome.cards)}")
    await ctx.ledger.record_run(JOB, ok=True, detail=f"user={profile.user_id}", now=now)


# ---------------------------------------------------------------------- stored analysis
def _outcome_key(key: LedgerKey) -> LedgerKey:
    return LedgerKey(OUTCOME_JOB, key.day, key.user_id, key.subject)


async def _outcome(ctx: PipelineContext, profile: UserProfile, key: LedgerKey) -> AnalysisOutcome:
    """The digest's analysis: the stored one on a retry, otherwise a fresh run (then stored)."""
    stored = await _load_outcome(ctx, key)
    if stored is not None:
        return stored
    day = key.day
    outcome = await AnalysisEngine(ctx).run(default_request(profile, day, day))
    await ctx.ledger.complete(_outcome_key(key), now=ctx.clock(), detail=_encode(outcome))
    return outcome


def _encode(outcome: AnalysisOutcome) -> str:
    return json.dumps(
        {
            "cards": [
                {
                    "id": card.record.id,
                    "no_value": card.no_value,
                    "watchlist": card.watchlist,
                    "risk_flags": list(card.risk_flags),
                    "data_as_of": None if card.data_as_of is None else card.data_as_of.isoformat(),
                }
                for card in outcome.cards
            ],
            "fixtures_analyzed": outcome.fixtures_analyzed,
            "matched": outcome.matched,
            "requested": outcome.requested,
            "api_requests_used": outcome.api_requests_used,
            "failed_fixtures": outcome.failed_fixtures,
            "skipped_no_price": outcome.skipped_no_price,
            "skipped_stale_price": outcome.skipped_stale_price,
            "data_stale": outcome.data_stale,
        }
    )


async def _load_outcome(ctx: PipelineContext, key: LedgerKey) -> AnalysisOutcome | None:
    """The analysis stored for ``key``, or ``None`` (none yet, or it cannot be read back)."""
    entry = await ctx.ledger.get(_outcome_key(key))
    if entry is None or not entry.detail:
        return None
    try:
        data = json.loads(entry.detail)
        cards = await _load_cards(ctx, data["cards"])
        if cards is None:  # a signal vanished: analyse again rather than send a gap
            return None
        return AnalysisOutcome(
            cards=cards,
            fixtures_analyzed=data["fixtures_analyzed"],
            matched=data["matched"],
            requested=data["requested"],
            api_requests_used=data["api_requests_used"],
            failed_fixtures=data["failed_fixtures"],
            skipped_no_price=data["skipped_no_price"],
            skipped_stale_price=data["skipped_stale_price"],
            data_stale=data["data_stale"],
        )
    except (KeyError, TypeError, ValueError) as error:  # damaged entry: just analyse again
        logger.warning("daily_outcome_unreadable", user_id=key.user_id, error=repr(error))
        return None


async def _load_cards(
    ctx: PipelineContext, items: list[dict[str, Any]]
) -> tuple[SignalCard, ...] | None:
    cards = []
    async with ctx.sessions() as session:
        for item in items:
            record = await get_signal(session, item["id"])
            if record is None:
                return None
            as_of = item["data_as_of"]
            cards.append(
                SignalCard(
                    record=record,
                    bookmaker=ctx.config.bet_bookmaker,
                    no_value=item["no_value"],
                    watchlist=item["watchlist"],
                    risk_flags=tuple(item["risk_flags"]),
                    data_as_of=None if as_of is None else datetime.fromisoformat(as_of),
                )
            )
    return tuple(cards)


def next_send_after(profile: UserProfile, now: datetime) -> datetime:
    """The next UTC instant this user's digest becomes due (for status displays)."""
    due = send_moment(profile, now)
    return due if due > now else due + timedelta(days=1)
