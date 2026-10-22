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

Failure modes. The job claims a ledger key per week, so a second call in the same week is a
no-op and a crash lets the lease expire and the next call retry. All writes are one transaction:
an interruption leaves the previous week's state untouched. Re-running on unchanged data writes
the same segment rows, no journal entries (nothing changed) and no duplicate calibration
version (an identical fit is recognised). A manual rollback of a segment change freezes that
segment for ``MANUAL_HOLD`` so the next review does not undo the person's decision.
"""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
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
    list_calibration_versions,
    list_segment_rows,
    mark_change_rolled_back,
    save_segment_row,
    set_active_calibration,
    settled_learning_signals,
)
from app.logging import get_logger
from app.models.asian import Settlement, settlement_returns
from app.models.calibration import (
    CalibrationFit,
    CalibrationReason,
    CalibrationSample,
    fit_calibration,
)
from app.models.segments import (
    SegmentAction,
    SegmentDecision,
    SegmentKey,
    SegmentObservation,
    SegmentState,
    SegmentStats,
    SegmentStatus,
    compute_segment_stats,
    segment_action,
)
from app.pipeline.context import PipelineContext
from app.pipeline.ledger import SYSTEM_USER, LedgerKey
from app.pipeline.overlay import segment_key

logger = get_logger(__name__)

JOB: Final = "learning"
SUBJECT: Final = "weekly"
MANUAL_HOLD: Final = timedelta(days=30)
"""How long a segment is left alone after a person rolled a change back."""

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
    now = ctx.clock()
    key = LedgerKey(JOB, week_start(now), SYSTEM_USER, SUBJECT)
    if not await ctx.ledger.claim(key, now=now):
        logger.info("learning_skipped", reason="already_done_this_week")
        return LearningReport(skipped=True)
    try:
        report = await _learn(ctx, now)
    except Exception as error:
        await ctx.ledger.fail(key, now=now, detail=repr(error)[:200])
        raise
    detail = f"signals={report.signals} segments={report.segments} changes={len(report.changes)}"
    await ctx.ledger.complete(key, now=now, detail=detail)
    logger.info("learning_done", job=JOB, signals=report.signals, changes=len(report.changes))
    return report


async def _learn(ctx: PipelineContext, now: datetime) -> LearningReport:
    async with ctx.sessions.begin() as session:
        signals = await settled_learning_signals(session, settled_before=now)
        stats = await asyncio.to_thread(compute_segment_stats, _observations(signals))
        changes = await _apply_segments(session, stats, _origins(signals), now)
        versions = await list_calibration_versions(session)
        samples = _samples(signals)
        fit = await asyncio.to_thread(
            fit_calibration,
            samples,
            cutoff=now,
            version=max((v.version for v in versions), default=0) + 1,
        )
        stored, calibration_change = await _apply_calibration(session, fit, versions, now)
    if calibration_change is not None:
        changes.append(calibration_change)
    return LearningReport(
        signals=len(signals),
        segments=len(stats),
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


def _samples(signals: Sequence[LearningSignal]) -> list[CalibrationSample]:
    return [
        CalibrationSample(s.probability, s.outcome in _PROFITABLE, s.settled_at) for s in signals
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
    stats: Mapping[SegmentKey, SegmentStats],
    origins: Mapping[SegmentKey, LearningSignal],
    now: datetime,
) -> list[LearningChange]:
    existing = {_row_key(row): row for row in await list_segment_rows(session)}
    changes = []
    for key, segment in stats.items():
        row = existing.get(key)
        status = SegmentStatus(
            _state_of(row),
            None if row is None else row.state_since,
            None if row is None else row.hold_until,
        )
        decision = segment_action(segment, status=status, now=now)
        updated = _updated_row(segment, decision, row, origins[key], now)
        await save_segment_row(session, updated, now=now)
        if decision.action is not SegmentAction.KEEP and decision.state is not status.state:
            changes.append(
                await _journal_segment(
                    session, row, updated, decision=decision, stats=segment, now=now
                )
            )
    return changes


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
    )


async def _journal_segment(  # noqa: PLR0913 - the facts of one journal entry
    session: AsyncSession,
    before: SegmentRecord | None,
    after: SegmentRecord,
    *,
    decision: SegmentDecision,
    stats: SegmentStats,
    now: datetime,
) -> LearningChange:
    ref = _segment_ref(after)
    evidence = {
        "n": stats.n,
        "roi": stats.roi.shrunk,
        "roi_high": stats.roi.high,
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
    )


# --------------------------------------------------------------------------- calibration


async def _apply_calibration(
    session: AsyncSession,
    fit: CalibrationFit,
    versions: Sequence[CalibrationRecord],
    now: datetime,
) -> tuple[int | None, LearningChange | None]:
    """Store the fit (unless it repeats the latest one) and activate it if it passed the gate."""
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
    if not fit.activate:
        return version.version, None
    previous = await active_calibration(session)
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
    )
    return version.version, change


# --------------------------------------------------------------------------- rollback


async def rollback_learning_change(ctx: PipelineContext, change_id: int) -> bool:
    """Undo one journal entry; ``False`` if it is unknown, already undone or not undoable.

    A calibration activation falls back to the version that was active before (or none); a
    segment change restores the previous state and freezes the segment for ``MANUAL_HOLD``.
    The rollback itself is journaled, and the original entry is marked as rolled back.
    """
    now = ctx.clock()
    async with ctx.sessions.begin() as session:
        change = await get_learning_change(session, change_id)
        if change is None or change.rolled_back:
            return False
        if change.kind == KIND_CALIBRATION_ACTIVATED:
            await _rollback_calibration(session, change, now)
        elif change.kind in _ACTION_KIND.values():
            if not await _rollback_segment(session, change, now):
                return False
        else:
            return False
        return await mark_change_rolled_back(session, change.id)


async def _rollback_calibration(
    session: AsyncSession, change: LearningChange, now: datetime
) -> None:
    previous = change.before.get("version")
    await set_active_calibration(session, previous)
    await add_learning_change(
        session,
        kind=KIND_CALIBRATION_ROLLED_BACK,
        subject=change.subject,
        before=change.after,
        after=change.before,
        reason=f"rollback of change {change.id}",
        now=now,
    )


async def _rollback_segment(session: AsyncSession, change: LearningChange, now: datetime) -> bool:
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
        return False
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
        hold_until=now + MANUAL_HOLD,
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
    )
    return True
