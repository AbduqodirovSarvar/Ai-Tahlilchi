"""Free-text notes users attach to their signals.

A note may be written before the match (``set_result_note`` only reaches settled signals),
and a signal can carry several. Every lookup includes ``user_id``: another user's signal
behaves as missing.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repo.records import SignalNoteRecord
from app.db.schema import SignalNoteRow, SignalRow


async def add_signal_note(
    session: AsyncSession, user_id: int, signal_id: int, text: str, *, created_at: datetime
) -> SignalNoteRecord | None:
    """Attach ``text`` to the user's own signal; ``None`` if it is unknown or not theirs."""
    owned = await session.scalar(
        select(SignalRow.id).where(SignalRow.id == signal_id, SignalRow.user_id == user_id)
    )
    if owned is None:
        return None
    row = SignalNoteRow(user_id=user_id, signal_id=signal_id, text=text, created_at=created_at)
    session.add(row)
    await session.flush()
    return _note(row)


async def list_signal_notes(
    session: AsyncSession, user_id: int, signal_id: int
) -> list[SignalNoteRecord]:
    """The user's notes on a signal, oldest first."""
    rows = await session.scalars(
        select(SignalNoteRow)
        .where(SignalNoteRow.user_id == user_id, SignalNoteRow.signal_id == signal_id)
        .order_by(SignalNoteRow.created_at, SignalNoteRow.id)
    )
    return [_note(row) for row in rows]


def _note(row: SignalNoteRow) -> SignalNoteRecord:
    return SignalNoteRecord(
        id=row.id,
        user_id=row.user_id,
        signal_id=row.signal_id,
        text=row.text,
        created_at=row.created_at,
    )
