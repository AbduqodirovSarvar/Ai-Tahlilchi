"""The weekly learning job (F8.5, F8.6): segments, calibration and the change journal.

Once a week (Monday, after settlement) the job

1. reads the settled signals (retro excluded, hidden included, one per fixture selection);
2. computes segment statistics (:mod:`app.models.segments`) and applies the guarded decision to
   every segment: raise its EV bar, disable it, or lift a restriction (written to
   ``segment_stats``);
3. fits a calibration on the *raw* probabilities (:mod:`app.models.calibration`) and activates
   it only if it beat the raw model on a later hold-out (written to ``calibration_versions``);
4. journals every change in ``learning_changes`` so it can be listed and rolled back.

The analysis path reads the result through :mod:`app.pipeline.overlay`.

Mode (``LEARNING_MODE``, :mod:`app.pipeline.learning_settings`): ``off`` does nothing; ``shadow``
(default) computes and stores everything but journals every change with ``applied=False`` and the
analysis path never reads it; ``apply`` journals real changes. Switching to ``apply`` is a human
decision after reviewing the shadow journal. The state stored while in ``shadow`` would become
effective the moment the mode flips, so :func:`promote_shadow_state` journals it as applied
changes first (decision #23): ``/ozgarishlar`` then lists, and can undo, everything that acts.

Failure modes. The job claims a ledger key per week, so a second call in the same week is a
no-op and a crash lets the lease expire and the next call retry. All writes are one transaction:
an interruption leaves the previous week's state untouched. Re-running on unchanged data writes
the same segment rows, no journal entries (nothing changed) and no duplicate calibration
version (an identical fit is recognised). A manual rollback of a segment change freezes that
segment, and a rollback of the calibration freezes new activations, for the configured hold so the
next review does not undo the person's decision. A rollback only restores state that the change
itself produced: if the segment or calibration has moved on since, it is refused as stale.
"""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import Outcome
from app.db.repo import (
    CalibrationRecord,
    LearningChange,
    LearningSignal,
    SegmentRecord,
    active_calibration,
    add_calibration_version,
    add_learning_change,
    get_learning_change,
    latest_change_of_kind,
    list_calibration_versions,
    list_segment_rows,
    mark_change_rolled_back,
    save_segment_row,
    set_active_calibration,
    settled_learning_signals,
)
from app.domain import Market
from app.logging import get_logger
from app.models.asian import Settlement, settlement_returns
from app.models.calibration import (
    CalibrationFit,
    CalibrationReason,
    CalibrationSample,
    confirmed,
    fit_calibration,
)
from app.models.segments import (
    SegmentAction,
    SegmentDecision,
    SegmentKey,
    SegmentObservation,
    SegmentReview,
    SegmentState,
    SegmentStats,
    SegmentStatus,
    review_segments,
)
from app.pipeline.context import PipelineContext
from app.pipeline.learning_settings import LearningMode
from app.pipeline.ledger import SYSTEM_USER, LedgerKey
from app.pipeline.overlay import segment_key

logger = get_logger(__name__)

JOB: Final = "learning"
SUBJECT: Final = "weekly"
MODE_JOB: Final = "learning.mode"
"""Ledger stamps ``shadow`` / ``apply``: when the job last ran in that mode."""
KIND_PROMOTED: Final = "shadow_promoted"
REFRESH_GROWTH: Final = 1.25
"""An active calibration is replaced only by a fit on this many times more samples."""

KIND_SEGMENT_RAISED: Final = "segment_raised"
KIND_SEGMENT_DISABLED: Final = "segment_disabled"
KIND_SEGMENT_RELAXED: Final = "segment_relaxed"
KIND_CALIBRATION_ACTIVATED: Final = "calibration_activated"
KIND_CALIBRATION_ROLLED_BACK: Final = "calibration_rolled_back"
KIND_SEGMENT_ROLLED_BACK: Final = "segment_rolled_back"

_SETTLEMENTS: Final = {
    Outcome.WIN: Settlement.WIN,
    Outcome.HALF_WIN: Settlement.HALF_WIN,
    Outcome.HALF_LOSS: Settlement.HALF_LOSS,
    Outcome.LOSS: Settlement.LOSS,
}
_PROFITABLE: Final = frozenset({Outcome.WIN, Outcome.HALF_WIN})
_ACTION_KIND: Final = {
    SegmentAction.RAISE_THRESHOLD: KIND_SEGMENT_RAISED,
    SegmentAction.DISABLE: KIND_SEGMENT_DISABLED,
    SegmentAction.REENABLE: KIND_SEGMENT_RELAXED,
}


@dataclass(frozen=True, slots=True)
class LearningReport:
    """What one run did (for logs, ``/holat`` and the weekly "what the model learned" report)."""

    skipped: bool = False
    signals: int = 0
    segments: int = 0
    changes: tuple[LearningChange, ...] = ()
    calibration_reason: CalibrationReason | None = None
    calibration_version: int | None = None
    calibration_activated: bool = False


def week_start(moment: datetime) -> date:
    """The Monday of the week of ``moment`` (the ledger day of the weekly job)."""
    return moment.date() - timedelta(days=moment.weekday())


async def run_learning(ctx: PipelineContext) -> LearningReport:
    """Run the weekly review once per week; later calls in the same week do nothing."""
    mode = ctx.learning_settings.mode
    if mode is LearningMode.OFF:
        logger.info("learning_skipped", reason="mode_off")
        return LearningReport(skipped=True)
    now = ctx.clock()
    if mode is LearningMode.APPLY:
        await promote_shadow_state(ctx)
    key = LedgerKey(JOB, week_start(now), SYSTEM_USER, SUBJECT)
    if not await ctx.ledger.claim(key, now=now):
        logger.info("learning_skipped", reason="already_done_this_week")
        return LearningReport(skipped=True)
    try:
        report = await _learn(ctx, now, applied=mode is LearningMode.APPLY)
    except Exception as error:
        await ctx.ledger.fail(key, now=now, detail=repr(error)[:200])
        raise
    detail = f"signals={report.signals} segments={report.segments} changes={len(report.changes)}"
    await ctx.ledger.complete(key, now=now, detail=detail)
    await ctx.ledger.stamp(MODE_JOB, mode.value, now=now)
    logger.info(
        "learning_done",
        job=JOB,
        mode=mode.value,
        signals=report.signals,
        changes=len(report.changes),
    )
    return report


async def promote_shadow_state(ctx: PipelineContext) -> int:
    """Journal the state stored while in ``shadow`` as applied changes (decision #23).

    Runs when the mode is ``apply`` and the job last ran in ``shadow`` (or never in ``apply``):
    every restricted segment and the active calibration become journal entries with
    ``applied=True`` (before: the neutral state), so the switch is visible and reversible.
    Idempotent: the ``apply`` stamp follows the writes, so a second call finds nothing to do (a
    crash in between repeats the entries, which stay individually undoable).

    Returns how many entries were written.
    """
    now = ctx.clock()
    applied_at = await ctx.ledger.stamped_at(MODE_JOB, LearningMode.APPLY.value)
    shadow_at = await ctx.ledger.stamped_at(MODE_JOB, LearningMode.SHADOW.value)
    if applied_at is not None and (shadow_at is None or shadow_at <= applied_at):
        return 0
    async with ctx.sessions.begin() as session:
        written = await _promote_segments(session, now) + await _promote_calibration(session, now)
    await ctx.ledger.stamp(MODE_JOB, LearningMode.APPLY.value, now=now)
    logger.info("learning_shadow_promoted", job=JOB, entries=written)
    return written


async def _promote_segments(session: AsyncSession, now: datetime) -> int:
    count = 0
    for row in await list_segment_rows(session):
        state = _state_of(row)
        if state is SegmentState.ACTIVE:
            continue
        ref = _segment_ref(row)
        await add_learning_change(
            session,
            kind=KIND_SEGMENT_DISABLED if state is SegmentState.DISABLED else KIND_SEGMENT_RAISED,
            subject=f"{row.league_ref}|{row.market.value}|{row.odds_bucket}",
            before=_snapshot(None, ref),
            after={**_snapshot(row, ref), "evidence": {"n": row.n, "reason": KIND_PROMOTED}},
            reason=KIND_PROMOTED,
            now=now,
            applied=True,
        )
        count += 1
    return count


async def _promote_calibration(session: AsyncSession, now: datetime) -> int:
    active = await active_calibration(session)
    if active is None:
        return 0
    await add_learning_change(
        session,
        kind=KIND_CALIBRATION_ACTIVATED,
        subject=f"calibration:{active.version}",
        before={"version": None},
        after={
            "version": active.version,
            "n_samples": active.n_samples,
            "brier_before": active.brier_before,
            "brier_after": active.brier_after,
        },
        reason=KIND_PROMOTED,
        now=now,
        applied=True,
    )
    return 1


async def _learn(ctx: PipelineContext, now: datetime, *, applied: bool) -> LearningReport:
    async with ctx.sessions.begin() as session:
        signals = await settled_learning_signals(session, settled_before=now)
        review, changes = await _apply_segments(session, signals, now, applied=applied)
        versions = await list_calibration_versions(session)
        fit = await asyncio.to_thread(
            fit_calibration,
            _samples(signals),
            cutoff=now,
            version=max((v.version for v in versions), default=0) + 1,
        )
        stored, calibration_change = await _apply_calibration(
            session, fit, versions, now, hold=ctx.learning_settings.manual_hold, applied=applied
        )
    if calibration_change is not None:
        changes.append(calibration_change)
    return LearningReport(
        signals=len(signals),
        segments=len(review.stats),
        changes=tuple(changes),
        calibration_reason=fit.reason,
        calibration_version=stored,
        calibration_activated=calibration_change is not None,
    )


# --------------------------------------------------------------------------- conversion


def _observations(signals: Sequence[LearningSignal]) -> list[SegmentObservation]:
    return [
        SegmentObservation(
            key=segment_key(s.sport, s.league_ref, s.market.type, s.odds),
            unit_return=settlement_returns(s.odds)[_SETTLEMENTS[s.outcome]],
            hit=s.outcome in _PROFITABLE,
            p_model=s.probability,
            block=s.kickoff.date().toordinal(),
            clv=s.clv,
        )
        for s in signals
    ]


def can_push(market: Market) -> bool:
    """True for a whole line (Total 2.0, handicap 0 or -1): the stake can come back unsettled."""
    return market.line is not None and float(market.line).is_integer()


def _samples(signals: Sequence[LearningSignal]) -> list[CalibrationSample]:
    """Calibration data. Markets that can push are left out (ADR 0013, addendum).

    A push is stored as void and void is not learned from, so for such a market the observed hit
    rate would be P(win | no push) while the model states P(win): comparing them would "teach" the
    calibration a bias that does not exist.
    """
    return [
        CalibrationSample(s.probability, s.outcome in _PROFITABLE, s.settled_at)
        for s in signals
        if not can_push(s.market)
    ]


def _origins(signals: Sequence[LearningSignal]) -> dict[SegmentKey, LearningSignal]:
    """One signal per segment: it carries the database identity (sport, league id, market)."""
    origins: dict[SegmentKey, LearningSignal] = {}
    for s in signals:
        origins.setdefault(segment_key(s.sport, s.league_ref, s.market.type, s.odds), s)
    return origins


# --------------------------------------------------------------------------- segments


def _row_key(row: SegmentRecord) -> SegmentKey:
    return SegmentKey(row.sport.value, str(row.league_ref), row.market.value, row.odds_bucket)


def _state_of(row: SegmentRecord | None) -> SegmentState:
    if row is None:
        return SegmentState.ACTIVE
    if not row.enabled:
        return SegmentState.DISABLED
    return SegmentState.RAISED if row.ev_threshold > 0.0 else SegmentState.ACTIVE


def _status_of(row: SegmentRecord | None) -> SegmentStatus:
    if row is None:
        return SegmentStatus()
    return SegmentStatus(_state_of(row), row.state_since, row.hold_until, row.flagged)


def _segment_ref(row: SegmentRecord) -> dict[str, Any]:
    return {
        "sport": row.sport.value,
        "league_id": row.league_id,
        "market": row.market.value,
        "odds_bucket": row.odds_bucket,
    }


def _snapshot(row: SegmentRecord | None, ref: dict[str, Any]) -> dict[str, Any]:
    return {
        **ref,
        "state": _state_of(row).value,
        "ev_threshold": 0.0 if row is None else row.ev_threshold,
        "enabled": True if row is None else row.enabled,
        "state_since": None
        if row is None or row.state_since is None
        else row.state_since.isoformat(),
    }


async def _apply_segments(
    session: AsyncSession,
    signals: Sequence[LearningSignal],
    now: datetime,
    *,
    applied: bool,
) -> tuple[SegmentReview, list[LearningChange]]:
    existing = {_row_key(row): row for row in await list_segment_rows(session)}
    statuses = {key: _status_of(row) for key, row in existing.items()}
    observations = _observations(signals)
    review = await asyncio.to_thread(review_segments, observations, statuses, now=now)
    origins = _origins(signals)
    changes = []
    for key, segment in review.stats.items():
        row = existing.get(key)
        decision = review.decisions[key]
        updated = _updated_row(segment, decision, row, origins[key], now)
        await save_segment_row(session, updated, now=now)
        if decision.action is not SegmentAction.KEEP and decision.state is not _state_of(row):
            changes.append(
                await _journal_segment(
                    session, row, updated, (decision, segment), now=now, applied=applied
                )
            )
    return review, changes


def _updated_row(
    stats: SegmentStats,
    decision: SegmentDecision,
    row: SegmentRecord | None,
    origin: LearningSignal,
    now: datetime,
) -> SegmentRecord:
    changed = decision.state is not _state_of(row)
    since = now if changed else (None if row is None else row.state_since)
    return SegmentRecord(
        sport=origin.sport,
        league_id=origin.league_id,
        league_ref=origin.league_ref,
        market=origin.market.type,
        odds_bucket=stats.key.odds_bucket,
        n=stats.n,
        roi=stats.roi.shrunk or 0.0,
        clv=stats.clv.shrunk,
        calib_bias=stats.calibration_bias.shrunk,
        ev_threshold=decision.ev_floor,
        enabled=decision.state is not SegmentState.DISABLED,
        state_since=None if decision.state is SegmentState.ACTIVE else since,
        hold_until=None if row is None or changed else row.hold_until,
        flagged=decision.flagged,
    )


async def _journal_segment(  # noqa: PLR0913 - the facts of one journal entry
    session: AsyncSession,
    before: SegmentRecord | None,
    after: SegmentRecord,
    verdict: tuple[SegmentDecision, SegmentStats],
    *,
    now: datetime,
    applied: bool,
) -> LearningChange:
    decision, stats = verdict
    ref = _segment_ref(after)
    evidence = {
        "n": stats.n,
        "roi": stats.roi.shrunk,
        "roi_high": stats.roi.high,
        "roi_p_value": stats.roi.p_value,
        "clv": stats.clv.shrunk,
        "clv_high": stats.clv.high,
        "reason": decision.reason.value,
    }
    return await add_learning_change(
        session,
        kind=_ACTION_KIND[decision.action],
        subject=f"{after.league_ref}|{after.market.value}|{after.odds_bucket}",
        before=_snapshot(before, ref),
        after={**_snapshot(after, ref), "evidence": evidence},
        reason=decision.reason.value,
        now=now,
        applied=applied,
    )


# --------------------------------------------------------------------------- calibration


async def _apply_calibration(  # noqa: PLR0913 - the facts of one weekly calibration step
    session: AsyncSession,
    fit: CalibrationFit,
    versions: Sequence[CalibrationRecord],
    now: datetime,
    *,
    hold: timedelta,
    applied: bool,
) -> tuple[int | None, LearningChange | None]:
    """Store the fit (unless it repeats the latest one); activate it if it earned that.

    A fit is activated only if it passed the gate on this run *and* the previous one, no person
    rolled a calibration back within ``hold``, and (when a map is already active) it rests on
    clearly more data than the active one.
    """
    version = fit.version
    if version is None:
        return None, None
    latest = versions[-1] if versions else None
    if latest is not None and (latest.n_samples, latest.params) == (
        version.n_samples,
        version.params,
    ):
        return latest.version, None
    await add_calibration_version(
        session,
        CalibrationRecord(
            version=version.version,
            method=version.method,
            params=dict(version.params),
            n_samples=version.n_samples,
            brier_before=version.brier_before,
            brier_after=version.brier_after,
            active=False,
            passed_gate=version.passed_gate,
            created_at=version.created_at,
        ),
    )
    previous_pass_at = latest.created_at if latest is not None and latest.passed_gate else None
    if not confirmed(fit, previous_pass_at=previous_pass_at):
        return version.version, None
    previous = await active_calibration(session)
    if await _frozen(session, now, hold) or _not_grown(previous, version.n_samples):
        return version.version, None
    await set_active_calibration(session, version.version)
    change = await add_learning_change(
        session,
        kind=KIND_CALIBRATION_ACTIVATED,
        subject=f"calibration:{version.version}",
        before={"version": None if previous is None else previous.version},
        after={
            "version": version.version,
            "n_samples": version.n_samples,
            "brier_before": version.brier_before,
            "brier_after": version.brier_after,
            "gain": fit.gain,
            "gain_low": fit.gain_low,
        },
        reason=fit.reason.value,
        now=now,
        applied=applied,
    )
    return version.version, change


async def _frozen(session: AsyncSession, now: datetime, hold: timedelta) -> bool:
    """True while a person's calibration rollback still forbids a new activation."""
    rolled_back = await latest_change_of_kind(session, KIND_CALIBRATION_ROLLED_BACK)
    return rolled_back is not None and now - rolled_back.created_at < hold


def _not_grown(active: CalibrationRecord | None, n_samples: int) -> bool:
    return active is not None and n_samples < active.n_samples * REFRESH_GROWTH


# --------------------------------------------------------------------------- rollback


class RollbackResult(StrEnum):
    """What :func:`rollback_change` did with a journal entry."""

    ROLLED_BACK = "rolled_back"
    NOT_FOUND = "not_found"
    ALREADY_ROLLED_BACK = "already_rolled_back"
    NOT_UNDOABLE = "not_undoable"
    STALE = "stale"
    """The state has moved on since this entry; undoing it would wipe newer state."""


async def rollback_change(ctx: PipelineContext, change_id: int) -> RollbackResult:
    """Undo one journal entry, but only if what it produced is still the current state.

    A calibration activation falls back to the version that was active before (or none) and
    freezes new activations; a segment change restores the previous state and freezes the
    segment, both for the configured hold. The rollback itself is journaled and the original
    entry is marked as rolled back. An older entry whose effect has since been superseded is
    refused as ``STALE`` instead of overwriting the newer state.
    """
    now = ctx.clock()
    hold = ctx.learning_settings.manual_hold
    async with ctx.sessions.begin() as session:
        change = await get_learning_change(session, change_id)
        if change is None:
            return RollbackResult.NOT_FOUND
        if change.rolled_back:
            return RollbackResult.ALREADY_ROLLED_BACK
        if change.kind == KIND_CALIBRATION_ACTIVATED:
            result = await _rollback_calibration(session, change, now)
        elif change.kind in _ACTION_KIND.values():
            result = await _rollback_segment(session, change, now, hold)
        else:
            return RollbackResult.NOT_UNDOABLE
        if result is RollbackResult.ROLLED_BACK:
            await mark_change_rolled_back(session, change.id)
        else:
            logger.warning("learning_rollback_refused", change_id=change_id, result=result.value)
        return result


async def _rollback_calibration(
    session: AsyncSession, change: LearningChange, now: datetime
) -> RollbackResult:
    active = await active_calibration(session)
    if (None if active is None else active.version) != change.after.get("version"):
        return RollbackResult.STALE
    await set_active_calibration(session, change.before.get("version"))
    await add_learning_change(
        session,
        kind=KIND_CALIBRATION_ROLLED_BACK,
        subject=change.subject,
        before=change.after,
        after=change.before,
        reason=f"rollback of change {change.id}",
        now=now,
        applied=change.applied,
    )
    return RollbackResult.ROLLED_BACK


def _same_restriction(row: SegmentRecord, snapshot: Mapping[str, Any]) -> bool:
    """The row still has the state the journal entry left it in."""
    return (_state_of(row).value, row.ev_threshold, row.enabled) == (
        snapshot.get("state"),
        snapshot.get("ev_threshold"),
        snapshot.get("enabled"),
    )


async def _rollback_segment(
    session: AsyncSession, change: LearningChange, now: datetime, hold: timedelta
) -> RollbackResult:
    ref = change.before
    rows = await list_segment_rows(session)
    row = next(
        (
            r
            for r in rows
            if (r.sport.value, r.league_id, r.market.value, r.odds_bucket)
            == (ref["sport"], ref["league_id"], ref["market"], ref["odds_bucket"])
        ),
        None,
    )
    if row is None:
        return RollbackResult.NOT_FOUND
    if not _same_restriction(row, change.after):
        return RollbackResult.STALE
    since = ref.get("state_since")
    restored = SegmentRecord(
        sport=row.sport,
        league_id=row.league_id,
        league_ref=row.league_ref,
        market=row.market,
        odds_bucket=row.odds_bucket,
        n=row.n,
        roi=row.roi,
        clv=row.clv,
        calib_bias=row.calib_bias,
        ev_threshold=ref["ev_threshold"],
        enabled=ref["enabled"],
        state_since=None if since is None else datetime.fromisoformat(since),
        hold_until=now + hold,
        flagged=False,
    )
    await save_segment_row(session, restored, now=now)
    await add_learning_change(
        session,
        kind=KIND_SEGMENT_ROLLED_BACK,
        subject=change.subject,
        before=change.after,
        after=change.before,
        reason=f"rollback of change {change.id}",
        now=now,
        applied=change.applied,
    )
    return RollbackResult.ROLLED_BACK
