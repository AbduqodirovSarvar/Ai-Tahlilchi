"""Scheduled recalculations (F4.5): 24 hours and N minutes before kick-off.

Recommending a signal queues two ``scheduled_recalcs`` tasks (see :mod:`.persist`).
:func:`run_due_recalcs` is called every minute: it claims due tasks (lease based, so a crash
loses nothing and two workers never take the same task), recalculates the signals with fresh
data, and tells the user.

Idempotency: before a message is sent its key ``(kind, local day, user, signal)`` is claimed
in the ledger; a task whose message was already delivered completes without sending again.
Quiet hours drop the *message* (never the recalculation, so the stored numbers stay fresh).
A 24-hour update is only worth a message when something changed; the pre-match update always
says "still valid" or "below the threshold".
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from app.db.enums import RecalcKind, SignalStatus, SignalVersionReason, WatchKind
from app.db.repo import (
    FixtureRecord,
    RecalcTask,
    SignalRecord,
    claim_due_recalcs,
    complete_recalc,
    get_signal,
    release_recalc,
)
from app.domain import OddsKind
from app.logging import get_logger
from app.pipeline.context import PipelineContext
from app.pipeline.daily import message_key
from app.pipeline.delivery import CancelDelivery, Delivery, UpdateDelivery, WatchDelivery
from app.pipeline.ledger import LedgerKey
from app.pipeline.profile import UserProfile, load_profile
from app.pipeline.provider import AUTOMATIC
from app.pipeline.queries import league_refs, team_refs
from app.pipeline.recalc import RecalcOptions, RecalcResult, RecalcVerdict, recalc_signals

logger = get_logger(__name__)

WATCH_LINEUPS: Final = "lineups"
WATCH_ODDS_MOVE: Final = "odds_move"
_OPTIONS: Final = {
    RecalcKind.BEFORE_24H: RecalcOptions(
        reason=SignalVersionReason.RECALC, purpose=AUTOMATIC, odds_kind=OddsKind.RECALC
    ),
    RecalcKind.PREMATCH: RecalcOptions(
        reason=SignalVersionReason.PREMATCH,
        purpose=AUTOMATIC,
        odds_kind=OddsKind.PREMATCH,
        check_lineups=True,
    ),
}


@dataclass(frozen=True, slots=True)
class PrematchReport:
    """Tasks handled by one tick."""

    notified: int = 0
    silent: int = 0
    skipped: int = 0
    failed: int = 0


async def run_due_recalcs(ctx: PipelineContext, delivery: Delivery) -> PrematchReport:
    """Process every due task (see the module docstring)."""
    now = ctx.clock()
    async with ctx.sessions.begin() as session:
        tasks = await claim_due_recalcs(session, now=now)
    counts = {"notified": 0, "silent": 0, "skipped": 0, "failed": 0}
    for task in tasks:
        outcome = await _handle(ctx, delivery, task)
        counts[outcome] += 1
    return PrematchReport(**counts)


async def _handle(ctx: PipelineContext, delivery: Delivery, task: RecalcTask) -> str:
    now = ctx.clock()
    async with ctx.sessions() as session:
        signal = await get_signal(session, task.signal_id)
    if signal is None or not _live(signal, now):
        await _finish(ctx, task)
        return "skipped"
    try:
        results, failed = await recalc_signals(ctx, [signal], _OPTIONS[task.kind])
        if failed or not results:
            raise RuntimeError("recalculation failed")  # noqa: TRY301, TRY003, EM101
        sent = await _notify(ctx, delivery, task, results[0])
    except Exception as error:  # the task stays claimable; one signal never blocks the rest
        logger.warning("prematch_failed", signal_id=task.signal_id, error=repr(error))
        async with ctx.sessions.begin() as session:
            await release_recalc(session, task.id)
        await ctx.ledger.record_error("prematch", f"signal {task.signal_id}: {error}", now=now)
        return "failed"
    await _finish(ctx, task)
    return "notified" if sent else "silent"


def _live(signal: SignalRecord, now: datetime) -> bool:
    """A task still matters: active, unsettled, not hidden, kick-off ahead."""
    return (
        signal.status is SignalStatus.ACTIVE
        and signal.result is None
        and signal.recommended_at is not None
        and signal.fixture.fixture.kickoff > now
    )


async def _finish(ctx: PipelineContext, task: RecalcTask) -> None:
    async with ctx.sessions.begin() as session:
        await complete_recalc(session, task.id, now=ctx.clock())


def _worth_a_message(kind: RecalcKind, result: RecalcResult, threshold: float) -> bool:
    if kind is RecalcKind.PREMATCH or result.verdict is not RecalcVerdict.STILL_VALID:
        return True
    before, after = result.before.record.odds, result.after.record.odds
    return abs(after / before - 1.0) >= threshold


async def _notify(
    ctx: PipelineContext, delivery: Delivery, task: RecalcTask, result: RecalcResult
) -> bool:
    """Deliver the result once; returns whether a message went out."""
    now = ctx.clock()
    record = result.after.record
    async with ctx.sessions() as session:
        profile = await load_profile(session, record.user_id)
    if profile.schedule.is_quiet(now):
        return False
    if not _worth_a_message(task.kind, result, ctx.config.odds_move_threshold):
        return False
    key = LedgerKey(
        task.kind.value, profile.schedule.local_day(now), record.user_id, str(record.id)
    )
    if not await ctx.ledger.claim(key, now=now):
        return False
    previous = await ctx.ledger.get(message_key(record.user_id, record.id))
    reply_to = None if previous is None else previous.detail
    try:
        reference = await _send(ctx, delivery, task, result, key, reply_to)
    except Exception:
        await ctx.ledger.fail(key, now=ctx.clock())
        raise
    await ctx.ledger.complete(key, now=ctx.clock(), detail=reference)
    await _watch_events(ctx, delivery, profile, result)
    return True


async def _send(  # noqa: PLR0913 - one value per fact of the message
    ctx: PipelineContext,
    delivery: Delivery,
    task: RecalcTask,
    result: RecalcResult,
    key: LedgerKey,
    reply_to: str | None,
) -> str | None:
    user_id = result.after.record.user_id
    if result.verdict is RecalcVerdict.CANCELLED:
        message = CancelDelivery(card=result.before, reason=result.cancel_reason or "cancelled")
        return await delivery.cancelled(user_id, message, key, reply_to)
    update = UpdateDelivery(
        kind=task.kind, result=result, lineups_confirmed=result.lineups_confirmed
    )
    return await delivery.update(user_id, update, key, reply_to)


async def _watch_events(
    ctx: PipelineContext, delivery: Delivery, profile: UserProfile, result: RecalcResult
) -> None:
    """Line-up and odds-move notifications for followed teams/leagues of this fixture."""
    fixture = result.after.record.fixture
    flags = await _watch_flags(ctx, profile, fixture)
    now = ctx.clock()
    wanted = []
    if WATCH_LINEUPS in flags and result.lineups_confirmed:
        wanted.append((WATCH_LINEUPS, "lineups confirmed"))
    before, after = result.before.record.odds, result.after.record.odds
    if WATCH_ODDS_MOVE in flags and abs(after / before - 1.0) >= ctx.config.odds_move_threshold:
        wanted.append((WATCH_ODDS_MOVE, f"{before:.2f} -> {after:.2f}"))
    day = profile.schedule.local_day(now)
    for event, detail in wanted:
        key = LedgerKey(f"watch.{event}", day, profile.user_id, str(fixture.id))
        if await ctx.ledger.claim(key, now=now):
            message = WatchDelivery(event=event, fixture=fixture, detail=detail, card=result.after)
            await delivery.watch_event(profile.user_id, message, key)
            await ctx.ledger.complete(key, now=ctx.clock())


async def watch_flags_for(
    ctx: PipelineContext, profile: UserProfile, fixture: FixtureRecord
) -> frozenset[str]:
    """Notification flags of the user's watchlist entries that concern ``fixture``."""
    return await _watch_flags(ctx, profile, fixture)


async def _watch_flags(
    ctx: PipelineContext, profile: UserProfile, fixture: FixtureRecord
) -> frozenset[str]:
    if not profile.watches:
        return frozenset()
    async with ctx.sessions() as session:
        leagues = await league_refs(session, profile.watch_league_ids)
        teams = await team_refs(session, profile.watch_team_ids)
    own = fixture.fixture
    flags: set[str] = set()
    for watch in profile.watches:
        if watch.kind is WatchKind.LEAGUE:
            ref = leagues.get(watch.entity_id)
            hit = ref is not None and ctx.leagues.canonical(ref) == ctx.leagues.canonical(
                own.league.ref
            )
        else:
            ref = teams.get(watch.entity_id)
            hit = ref in (own.home.ref, own.away.ref)
        if hit:
            flags.update(watch.notify_flags)
    return frozenset(flags)
