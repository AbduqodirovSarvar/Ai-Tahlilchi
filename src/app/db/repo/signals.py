"""Signals, their version history and their settlement.

Identity rule (service contract): one signal per user + fixture + market + line +
selection (the table's unique key). Re-running an analysis updates that row and appends a
``signal_versions`` entry; it never creates a duplicate.

``recommended_at`` is written once: the first time a draft arrives with a recommendation
time, together with the thresholds of that moment. Later drafts never move or clear it.
A retro draft (fixture already started) never overwrites a non-retro signal.

Reports read only recommended, non-retro signals and **include hidden ones** (hiding is a
view preference, not a selection decision).
"""

from collections.abc import Collection
from datetime import datetime

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db.enums import ResultSource, SignalSource, SignalStatus, SignalVersionReason
from app.db.mappers import line_to_db, market_from_db
from app.db.repo.fixtures import to_record
from app.db.repo.records import (
    SignalDraft,
    SignalRecord,
    SignalResultRecord,
    SignalVersionRecord,
    Thresholds,
)
from app.db.schema import FixtureRow, SignalResultRow, SignalRow, SignalVersionRow, utcnow

_EAGER = (
    joinedload(SignalRow.fixture).joinedload(FixtureRow.league),
    joinedload(SignalRow.fixture).joinedload(FixtureRow.home_team),
    joinedload(SignalRow.fixture).joinedload(FixtureRow.away_team),
)


async def upsert_signal(
    session: AsyncSession,
    draft: SignalDraft,
    *,
    reason: SignalVersionReason = SignalVersionReason.INITIAL,
) -> SignalRecord:
    """Create or update the signal for ``draft`` and record a new version of its numbers.

    A new signal always gets version 1 with reason ``initial``. Runs in the caller's
    transaction (flushes, does not commit).
    """
    row = await session.scalar(
        select(SignalRow).where(
            SignalRow.user_id == draft.user_id,
            SignalRow.fixture_id == draft.fixture_id,
            SignalRow.market == draft.market.type,
            SignalRow.selection == draft.selection,
            SignalRow.line == line_to_db(draft.market),
        )
    )
    if row is None:
        row = _new_row(draft)
        session.add(row)
        await session.flush()
        version, reason = 1, SignalVersionReason.INITIAL
    elif row.source is not SignalSource.RETRO and draft.source is SignalSource.RETRO:
        return await _require(session, row.id)
    else:
        _update_row(row, draft)
        version = await _latest_version(session, row.id) + 1
    session.add(
        SignalVersionRow(
            signal_id=row.id,
            version=version,
            odds=draft.odds,
            p_model=draft.p_model,
            ev=draft.ev,
            reason=reason,
        )
    )
    await session.flush()
    return await _require(session, row.id)


def _new_row(draft: SignalDraft) -> SignalRow:
    row = SignalRow(
        user_id=draft.user_id,
        fixture_id=draft.fixture_id,
        market=draft.market.type,
        selection=draft.selection,
        line=line_to_db(draft.market),
        source=draft.source,
        status=SignalStatus.ACTIVE,
        recommended_at=draft.recommended_at,
    )
    _set_numbers(row, draft)
    _set_thresholds(row, draft.thresholds)
    return row


def _update_row(row: SignalRow, draft: SignalDraft) -> None:
    _set_numbers(row, draft)
    if row.source is SignalSource.RETRO and draft.source is not SignalSource.RETRO:
        row.source = draft.source
    if row.recommended_at is None and draft.recommended_at is not None:
        row.recommended_at = draft.recommended_at
        _set_thresholds(row, draft.thresholds)
    elif None in (row.min_prob, row.min_ev, row.odds_min, row.odds_max):  # pre-0003 rows
        _set_thresholds(row, draft.thresholds)


def _set_numbers(row: SignalRow, draft: SignalDraft) -> None:
    row.odds = draft.odds
    row.p_model = draft.p_model
    row.ev = draft.ev
    row.confidence = draft.confidence
    row.stake_units = draft.stake_units
    if draft.llm_verdict is not None or draft.llm_note is not None:
        row.llm_verdict = draft.llm_verdict
        row.llm_note = draft.llm_note


def _set_thresholds(row: SignalRow, thresholds: Thresholds) -> None:
    row.min_prob = thresholds.min_prob
    row.min_ev = thresholds.min_ev
    row.odds_min = thresholds.odds_min
    row.odds_max = thresholds.odds_max


async def _latest_version(session: AsyncSession, signal_id: int) -> int:
    latest = await session.scalar(
        select(func.max(SignalVersionRow.version)).where(SignalVersionRow.signal_id == signal_id)
    )
    return latest or 0


async def _require(session: AsyncSession, signal_id: int) -> SignalRecord:
    records = await _load(session, SignalRow.id == signal_id)
    return records[0]


async def get_signal(
    session: AsyncSession, signal_id: int, *, user_id: int | None = None
) -> SignalRecord | None:
    """A signal by id; with ``user_id``, only if it belongs to that user."""
    conditions = [SignalRow.id == signal_id]
    if user_id is not None:
        conditions.append(SignalRow.user_id == user_id)
    records = await _load(session, *conditions)
    return records[0] if records else None


async def list_user_signals(  # noqa: PLR0913 - explicit filters instead of a query object
    session: AsyncSession,
    user_id: int,
    *,
    start: datetime,
    end: datetime,
    include_hidden: bool = False,
    include_retro: bool = False,
) -> list[SignalRecord]:
    """A user's signals for fixtures kicking off in ``[start, end)``, by kick-off."""
    conditions = [
        SignalRow.user_id == user_id,
        SignalRow.fixture_id.in_(
            select(FixtureRow.id).where(FixtureRow.kickoff_at >= start, FixtureRow.kickoff_at < end)
        ),
    ]
    if not include_hidden:
        conditions.append(SignalRow.status != SignalStatus.HIDDEN)
    if not include_retro:
        conditions.append(SignalRow.source != SignalSource.RETRO)
    return await _load(session, *conditions)


async def report_signals(
    session: AsyncSession,
    user_id: int,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[SignalRecord]:
    """Signals that count in reports: recommended, not retro, hidden ones included.

    ``start``/``end`` bound the fixture kick-off (``[start, end)``; ``None`` = open).
    Used for report, calibration, day summary, export and pending results alike.
    """
    fixture_window = select(FixtureRow.id)
    if start is not None:
        fixture_window = fixture_window.where(FixtureRow.kickoff_at >= start)
    if end is not None:
        fixture_window = fixture_window.where(FixtureRow.kickoff_at < end)
    return await _load(
        session,
        SignalRow.user_id == user_id,
        SignalRow.recommended_at.is_not(None),
        SignalRow.source != SignalSource.RETRO,
        SignalRow.fixture_id.in_(fixture_window),
    )


async def signals_for_fixtures(
    session: AsyncSession,
    fixture_ids: Collection[int],
    *,
    statuses: Collection[SignalStatus] = (SignalStatus.ACTIVE,),
) -> list[SignalRecord]:
    """Signals of every user on ``fixture_ids`` (recalc, pre-match, settlement)."""
    if not fixture_ids:
        return []
    return await _load(
        session,
        SignalRow.fixture_id.in_(list(fixture_ids)),
        SignalRow.status.in_(list(statuses)),
        SignalRow.source != SignalSource.RETRO,
    )


async def unsettled_signals(
    session: AsyncSession, *, kicked_off_before: datetime
) -> list[SignalRecord]:
    """Recommended, non-retro signals without a result whose fixture has kicked off."""
    return await _load(
        session,
        SignalRow.recommended_at.is_not(None),
        SignalRow.source != SignalSource.RETRO,
        SignalRow.id.not_in(select(SignalResultRow.signal_id)),
        SignalRow.fixture_id.in_(
            select(FixtureRow.id).where(FixtureRow.kickoff_at < kicked_off_before)
        ),
    )


async def set_signal_status(
    session: AsyncSession, signal_id: int, status: SignalStatus, *, user_id: int | None = None
) -> bool:
    """Change a signal's status; ``False`` if it does not exist (for that user)."""
    statement = update(SignalRow).where(SignalRow.id == signal_id)
    if user_id is not None:
        statement = statement.where(SignalRow.user_id == user_id)
    result = await session.execute(statement.values(status=status, updated_at=utcnow()))
    return bool(getattr(result, "rowcount", 0))


async def signal_versions(session: AsyncSession, signal_id: int) -> list[SignalVersionRecord]:
    """Every version of a signal's numbers, oldest first."""
    rows = await session.scalars(
        select(SignalVersionRow)
        .where(SignalVersionRow.signal_id == signal_id)
        .order_by(SignalVersionRow.version)
    )
    return [
        SignalVersionRecord(
            signal_id=row.signal_id,
            version=row.version,
            odds=row.odds,
            p_model=row.p_model,
            ev=row.ev,
            reason=row.reason,
            created_at=row.created_at,
        )
        for row in rows
    ]


async def settle_signal(
    session: AsyncSession, settlement: SignalResultRecord
) -> SignalResultRecord:
    """Store a signal's settlement (one per signal).

    A manual settlement is never overwritten by an automatic (``api``) one; any other
    combination replaces the stored values (corrections). The user note is kept unless
    the new settlement carries one.
    """
    row = await session.scalar(
        select(SignalResultRow).where(SignalResultRow.signal_id == settlement.signal_id)
    )
    if row is None:
        row = SignalResultRow(signal_id=settlement.signal_id)
        session.add(row)
    elif row.source is ResultSource.MANUAL and settlement.source is ResultSource.API:
        return _result_record(row)
    row.outcome = settlement.outcome
    row.source = settlement.source
    row.profit_units = settlement.profit_units
    row.closing_odds = settlement.closing_odds
    row.clv = settlement.clv
    row.settled_at = settlement.settled_at
    if settlement.user_note is not None:
        row.user_note = settlement.user_note
    await session.flush()
    return _result_record(row)


async def set_result_note(session: AsyncSession, signal_id: int, note: str) -> bool:
    """Attach the user's note to a settled signal; ``False`` if it is not settled yet."""
    result = await session.execute(
        update(SignalResultRow).where(SignalResultRow.signal_id == signal_id).values(user_note=note)
    )
    return bool(getattr(result, "rowcount", 0))


# --------------------------------------------------------------------------- loading


async def _load(session: AsyncSession, *conditions: ColumnElement[bool]) -> list[SignalRecord]:
    """Signals matching ``conditions`` with fixtures, versions and results: 3 queries."""
    rows = (
        (
            await session.scalars(
                select(SignalRow)
                .options(*_EAGER)
                .where(*conditions)
                .order_by(SignalRow.created_at, SignalRow.id)
            )
        )
        .unique()
        .all()
    )
    ids = [row.id for row in rows]
    if not ids:
        return []
    version_rows = await session.execute(
        select(SignalVersionRow.signal_id, func.max(SignalVersionRow.version))
        .where(SignalVersionRow.signal_id.in_(ids))
        .group_by(SignalVersionRow.signal_id)
    )
    versions: dict[int, int] = dict(version_rows.all())
    results = {
        row.signal_id: _result_record(row)
        for row in await session.scalars(
            select(SignalResultRow).where(SignalResultRow.signal_id.in_(ids))
        )
    }
    records = [_record(row, versions.get(row.id, 0), results.get(row.id)) for row in rows]
    records.sort(key=lambda record: (record.fixture.fixture.kickoff, record.id))
    return records


def _record(row: SignalRow, version: int, result: SignalResultRecord | None) -> SignalRecord:
    thresholds = None
    low, high = row.odds_min, row.odds_max
    if row.min_prob is not None and row.min_ev is not None and low is not None and high is not None:
        thresholds = Thresholds(row.min_prob, row.min_ev, low, high)
    return SignalRecord(
        id=row.id,
        user_id=row.user_id,
        fixture=to_record(row.fixture),
        market=market_from_db(row.market, row.line),
        selection=row.selection,
        odds=row.odds,
        p_model=row.p_model,
        ev=row.ev,
        confidence=row.confidence,
        stake_units=row.stake_units,
        llm_verdict=row.llm_verdict,
        llm_note=row.llm_note,
        source=row.source,
        status=row.status,
        recommended_at=row.recommended_at,
        thresholds=thresholds,
        version=version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        result=result,
    )


def _result_record(row: SignalResultRow) -> SignalResultRecord:
    return SignalResultRecord(
        signal_id=row.signal_id,
        outcome=row.outcome,
        source=row.source,
        profit_units=row.profit_units,
        closing_odds=row.closing_odds,
        clv=row.clv,
        user_note=row.user_note,
        settled_at=row.settled_at,
    )
