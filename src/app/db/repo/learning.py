"""Reads and writes of the learning loop: settled signals, segment rows, calibrations, journal.

The weekly job (``app.pipeline.learning``) is the only writer. Everything runs in the caller's
transaction (flush, never commit), like the rest of the repository.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import Outcome, SignalSource
from app.db.mappers import market_from_db
from app.db.schema import (
    CalibrationVersionRow,
    FixtureRow,
    LeagueRow,
    LearningChangeRow,
    SegmentStatRow,
    SignalResultRow,
    SignalRow,
)
from app.domain import ExternalRef, Market, MarketType, Sport

LEARNING_OUTCOMES = (Outcome.WIN, Outcome.HALF_WIN, Outcome.HALF_LOSS, Outcome.LOSS)
"""Results that carry information about the model; void and cash-out do not."""


@dataclass(frozen=True, slots=True, kw_only=True)
class LearningSignal:
    """A settled signal as the learning loop reads it (one per fixture selection, all users)."""

    signal_id: int
    sport: Sport
    league_id: int
    league_ref: ExternalRef
    market: Market
    odds: float
    """The first recommended odds (decision #17)."""
    probability: float
    """The raw (uncalibrated) model probability."""
    outcome: Outcome
    clv: float | None
    kickoff: datetime
    settled_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class SegmentRecord:
    """A stored segment row with its league reference."""

    sport: Sport
    league_id: int
    league_ref: ExternalRef
    market: MarketType
    odds_bucket: str
    n: int
    roi: float
    clv: float | None
    calib_bias: float | None
    ev_threshold: float
    enabled: bool
    state_since: datetime | None
    hold_until: datetime | None
    flagged: bool = False
    """Tightening evidence was seen on the previous weekly run (it must repeat to act)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class CalibrationRecord:
    """A stored calibration version."""

    version: int
    method: str
    params: dict[str, Any]
    n_samples: int
    brier_before: float
    brier_after: float
    active: bool
    passed_gate: bool
    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class LearningChange:
    """One entry of the change journal."""

    id: int
    created_at: datetime
    kind: str
    subject: str
    before: dict[str, Any]
    after: dict[str, Any]
    reason: str
    rolled_back: bool
    applied: bool = True
    """False for a shadow-mode entry: what the loop would have done, not what it did."""


# --------------------------------------------------------------------------- settled signals


async def settled_learning_signals(
    session: AsyncSession, *, settled_before: datetime
) -> list[LearningSignal]:
    """Recommended, non-retro signals (hidden included) settled win/loss before the cutoff.

    One entry per fixture selection: the same pick delivered to several users is one event, so
    the lowest signal id wins. Ordered by settlement time, then id.
    """
    statement = (
        select(SignalRow, SignalResultRow, FixtureRow, LeagueRow)
        .join(SignalResultRow, SignalResultRow.signal_id == SignalRow.id)
        .join(FixtureRow, FixtureRow.id == SignalRow.fixture_id)
        .join(LeagueRow, LeagueRow.id == FixtureRow.league_id)
        .where(
            SignalRow.recommended_at.is_not(None),
            SignalRow.source != SignalSource.RETRO,
            SignalResultRow.outcome.in_(LEARNING_OUTCOMES),
            SignalResultRow.settled_at < settled_before,
        )
        .order_by(SignalRow.id)
    )
    seen: set[tuple[int, MarketType, object, float]] = set()
    found: list[LearningSignal] = []
    for signal, result, fixture, league in (await session.execute(statement)).all():
        key = (signal.fixture_id, signal.market, signal.selection, signal.line)
        if key in seen:
            continue
        seen.add(key)
        found.append(_learning_signal(signal, result, fixture, league))
    found.sort(key=lambda item: (item.settled_at, item.signal_id))
    return found


def _learning_signal(
    signal: SignalRow, result: SignalResultRow, fixture: FixtureRow, league: LeagueRow
) -> LearningSignal:
    return LearningSignal(
        signal_id=signal.id,
        sport=league.sport,
        league_id=league.id,
        league_ref=ExternalRef(league.source, league.external_id),
        market=market_from_db(signal.market, signal.line),
        odds=signal.odds if signal.recommended_odds is None else signal.recommended_odds,
        probability=signal.p_model if signal.p_raw is None else signal.p_raw,
        outcome=result.outcome,
        clv=result.clv,
        kickoff=fixture.kickoff_at,
        settled_at=result.settled_at,
    )


# --------------------------------------------------------------------------- segments


async def list_segment_rows(session: AsyncSession) -> list[SegmentRecord]:
    """Every stored segment row, in key order."""
    rows = await session.execute(
        select(SegmentStatRow, LeagueRow)
        .join(LeagueRow, LeagueRow.id == SegmentStatRow.league_id)
        .order_by(SegmentStatRow.id)
    )
    return [_segment(row, league) for row, league in rows.all()]


async def save_segment_row(session: AsyncSession, record: SegmentRecord, *, now: datetime) -> None:
    """Insert or update the row of ``record``'s segment (its unique key is the identity)."""
    row = await session.scalar(
        select(SegmentStatRow).where(
            SegmentStatRow.sport == record.sport,
            SegmentStatRow.league_id == record.league_id,
            SegmentStatRow.market == record.market,
            SegmentStatRow.odds_bucket == record.odds_bucket,
        )
    )
    if row is None:
        row = SegmentStatRow(
            sport=record.sport,
            league_id=record.league_id,
            market=record.market,
            odds_bucket=record.odds_bucket,
        )
        session.add(row)
    row.n = record.n
    row.roi = record.roi
    row.clv = record.clv
    row.calib_bias = record.calib_bias
    row.ev_threshold = record.ev_threshold
    row.enabled = record.enabled
    row.state_since = record.state_since
    row.hold_until = record.hold_until
    row.flagged = record.flagged
    row.updated_at = now
    await session.flush()


def _segment(row: SegmentStatRow, league: LeagueRow) -> SegmentRecord:
    return SegmentRecord(
        sport=row.sport,
        league_id=row.league_id,
        league_ref=ExternalRef(league.source, league.external_id),
        market=row.market,
        odds_bucket=row.odds_bucket,
        n=row.n,
        roi=row.roi,
        clv=row.clv,
        calib_bias=row.calib_bias,
        ev_threshold=row.ev_threshold,
        enabled=row.enabled,
        state_since=row.state_since,
        hold_until=row.hold_until,
        flagged=row.flagged,
    )


# --------------------------------------------------------------------------- calibration


async def list_calibration_versions(session: AsyncSession) -> list[CalibrationRecord]:
    """All stored calibration versions, oldest first."""
    rows = await session.scalars(
        select(CalibrationVersionRow).order_by(CalibrationVersionRow.version)
    )
    return [_calibration(row) for row in rows]


async def active_calibration(session: AsyncSession) -> CalibrationRecord | None:
    """The active version that passed its gate (the newest if, against the invariant, several).

    A version that never passed the hold-out gate is never trusted, even if flagged active.
    """
    row = await session.scalar(
        select(CalibrationVersionRow)
        .where(CalibrationVersionRow.active.is_(True), CalibrationVersionRow.passed_gate.is_(True))
        .order_by(CalibrationVersionRow.version.desc())
    )
    return None if row is None else _calibration(row)


async def add_calibration_version(session: AsyncSession, record: CalibrationRecord) -> None:
    """Store a new version; ``record.active`` is honoured via :func:`set_active_calibration`."""
    session.add(
        CalibrationVersionRow(
            version=record.version,
            method=record.method,
            params=record.params,
            n_samples=record.n_samples,
            brier_before=record.brier_before,
            brier_after=record.brier_after,
            active=False,
            passed_gate=record.passed_gate,
            created_at=record.created_at,
        )
    )
    await session.flush()
    if record.active:
        await set_active_calibration(session, record.version)


async def set_active_calibration(session: AsyncSession, version: int | None) -> None:
    """Make ``version`` the only active one; ``None`` deactivates all (no calibration)."""
    await session.execute(update(CalibrationVersionRow).values(active=False))
    if version is not None:
        await session.execute(
            update(CalibrationVersionRow)
            .where(CalibrationVersionRow.version == version)
            .values(active=True)
        )


def _calibration(row: CalibrationVersionRow) -> CalibrationRecord:
    return CalibrationRecord(
        version=row.version,
        method=row.method,
        params=row.params,
        n_samples=row.n_samples,
        brier_before=row.brier_before,
        brier_after=row.brier_after,
        active=row.active,
        passed_gate=row.passed_gate,
        created_at=row.created_at,
    )


# --------------------------------------------------------------------------- journal


async def add_learning_change(  # noqa: PLR0913 - one column per argument
    session: AsyncSession,
    *,
    kind: str,
    subject: str,
    before: dict[str, Any],
    after: dict[str, Any],
    reason: str,
    now: datetime,
    applied: bool = True,
) -> LearningChange:
    """Append an entry to the change journal (``applied=False`` marks a shadow entry)."""
    row = LearningChangeRow(
        kind=kind,
        subject=subject,
        before=before,
        after=after,
        reason=reason,
        created_at=now,
        applied=applied,
    )
    session.add(row)
    await session.flush()
    return _change(row)


async def list_learning_changes(
    session: AsyncSession,
    *,
    limit: int = 20,
    since: datetime | None = None,
    applied: bool | None = None,
) -> list[LearningChange]:
    """Journal entries, newest first; ``since`` and ``applied`` (shadow vs real) narrow them."""
    statement = select(LearningChangeRow).order_by(
        LearningChangeRow.created_at.desc(), LearningChangeRow.id.desc()
    )
    if since is not None:
        statement = statement.where(LearningChangeRow.created_at >= since)
    if applied is not None:
        statement = statement.where(LearningChangeRow.applied.is_(applied))
    return [_change(row) for row in await session.scalars(statement.limit(limit))]


async def latest_change_of_kind(session: AsyncSession, kind: str) -> LearningChange | None:
    """The newest journal entry of ``kind`` (shadow or not), or ``None``."""
    row = await session.scalar(
        select(LearningChangeRow)
        .where(LearningChangeRow.kind == kind)
        .order_by(LearningChangeRow.created_at.desc(), LearningChangeRow.id.desc())
        .limit(1)
    )
    return None if row is None else _change(row)


async def get_learning_change(session: AsyncSession, change_id: int) -> LearningChange | None:
    """One journal entry, or ``None``."""
    row = await session.get(LearningChangeRow, change_id)
    return None if row is None else _change(row)


async def mark_change_rolled_back(session: AsyncSession, change_id: int) -> bool:
    """Flag an entry as rolled back; ``False`` if it is unknown or already flagged."""
    result = await session.execute(
        update(LearningChangeRow)
        .where(LearningChangeRow.id == change_id, LearningChangeRow.rolled_back.is_(False))
        .values(rolled_back=True)
    )
    return bool(getattr(result, "rowcount", 0))


def _change(row: LearningChangeRow) -> LearningChange:
    return LearningChange(
        id=row.id,
        created_at=row.created_at,
        kind=row.kind,
        subject=row.subject,
        before=row.before,
        after=row.after,
        reason=row.reason,
        rolled_back=row.rolled_back,
        applied=row.applied,
    )
