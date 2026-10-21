"""Odds reads: the current price per selection and full price histories."""

from collections.abc import Collection, Iterable, Sequence
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.mappers import odds_from_row
from app.db.schema import FixtureRow, OddsSnapshotRow
from app.domain import ExternalRef, OddsKind, OddsSnapshot

# Default preference for "the price to use now": a closing price once it exists, then the
# most recent kind of capture, the opening price last.
DEFAULT_KIND_PRIORITY: Final = (
    OddsKind.CLOSING,
    OddsKind.PREMATCH,
    OddsKind.RECALC,
    OddsKind.MORNING,
    OddsKind.OPENING,
)

type _Identity = tuple[int, str, str, str, float]


async def latest_odds(
    session: AsyncSession,
    fixtures: Iterable[ExternalRef],
    *,
    kinds: Sequence[OddsKind] = DEFAULT_KIND_PRIORITY,
    bookmakers: Collection[str] = (),
) -> dict[ExternalRef, list[OddsSnapshot]]:
    """One price per (fixture, bookmaker, market, line, selection).

    The first kind in ``kinds`` that has a price wins; within a kind the latest
    ``taken_at`` wins. Kinds not listed are ignored (``kinds=(OddsKind.CLOSING,)`` gives
    closing prices only). One query for all fixtures.
    """
    rows, refs = await _odds_rows(session, fixtures, kinds, bookmakers)
    priority = {kind: index for index, kind in enumerate(kinds)}
    best: dict[_Identity, OddsSnapshotRow] = {}
    for row in rows:
        key = (row.fixture_id, row.bookmaker, row.market.value, row.selection.value, row.line)
        current = best.get(key)
        if current is None or (priority[row.kind], -row.taken_at.timestamp()) < (
            priority[current.kind],
            -current.taken_at.timestamp(),
        ):
            best[key] = row
    result: dict[ExternalRef, list[OddsSnapshot]] = {ref: [] for ref in refs.values()}
    for row in sorted(best.values(), key=_display_order):
        ref = refs[row.fixture_id]
        result[ref].append(odds_from_row(row, fixture=ref))
    return result


async def odds_history(
    session: AsyncSession,
    fixture: ExternalRef,
    *,
    kinds: Sequence[OddsKind] = tuple(OddsKind),
    bookmakers: Collection[str] = (),
) -> list[OddsSnapshot]:
    """Every stored price of one fixture, oldest first (line movement, CLV)."""
    rows, _ = await _odds_rows(session, [fixture], kinds, bookmakers)
    ordered = sorted(rows, key=lambda row: (row.taken_at, *_display_order(row)))
    return [odds_from_row(row, fixture=fixture) for row in ordered]


async def _odds_rows(
    session: AsyncSession,
    fixtures: Iterable[ExternalRef],
    kinds: Sequence[OddsKind],
    bookmakers: Collection[str],
) -> tuple[list[OddsSnapshotRow], dict[int, ExternalRef]]:
    wanted = set(fixtures)
    if not wanted or not kinds:
        return [], {}
    id_rows = await session.execute(
        select(FixtureRow.id, FixtureRow.source, FixtureRow.external_id).where(
            FixtureRow.external_id.in_({ref.external_id for ref in wanted})
        )
    )
    refs = {
        row_id: ExternalRef(source, external_id)
        for row_id, source, external_id in id_rows
        if ExternalRef(source, external_id) in wanted
    }
    if not refs:
        return [], {}
    query = select(OddsSnapshotRow).where(
        OddsSnapshotRow.fixture_id.in_(list(refs)), OddsSnapshotRow.kind.in_(list(kinds))
    )
    if bookmakers:
        query = query.where(OddsSnapshotRow.bookmaker.in_(list(bookmakers)))
    return list((await session.scalars(query)).all()), refs


def _display_order(row: OddsSnapshotRow) -> tuple[str, str, float, str]:
    return (row.bookmaker, row.market.value, row.line, row.selection.value)
