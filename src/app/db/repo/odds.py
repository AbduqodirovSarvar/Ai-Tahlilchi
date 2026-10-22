"""Odds reads: the current price per selection and full price histories."""

from collections.abc import Collection, Iterable, Sequence
from datetime import datetime
from typing import Final

from sqlalchemy import or_, select
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

type _Group = tuple[int, str, str, float]  # fixture, bookmaker, market, line


async def latest_odds(
    session: AsyncSession,
    fixtures: Iterable[ExternalRef],
    *,
    kinds: Sequence[OddsKind] = DEFAULT_KIND_PRIORITY,
    bookmakers: Collection[str] = (),
    taken_before: datetime | None = None,
) -> dict[ExternalRef, list[OddsSnapshot]]:
    """One price per (fixture, bookmaker, market, line, selection).

    The kind is chosen **per market line of a bookmaker**, never per selection: all
    selections of one market come from the same kind, so the prices form a coherent set
    (de-vigging mixed closing and opening prices would give wrong probabilities). The
    group takes the first kind in ``kinds`` that prices every selection seen in the group
    (any kind, listed ones only); if none is complete, the kind with most selections (ties:
    earlier in ``kinds``). Within the kind the latest ``taken_at`` per selection wins. Kinds
    not listed are ignored (``kinds=(OddsKind.CLOSING,)`` gives closing prices only). One
    query for all fixtures. With ``taken_before`` only snapshots taken earlier count, except
    ``closing`` ones (a late re-sent price cannot replace the one before kick-off).
    """
    rows, refs = await _odds_rows(session, fixtures, kinds, bookmakers, taken_before)
    priority = {kind: index for index, kind in enumerate(kinds)}
    groups: dict[_Group, dict[OddsKind, dict[str, OddsSnapshotRow]]] = {}
    for row in rows:
        group = (row.fixture_id, row.bookmaker, row.market.value, row.line)
        by_selection = groups.setdefault(group, {}).setdefault(row.kind, {})
        current = by_selection.get(row.selection.value)
        if current is None or row.taken_at > current.taken_at:
            by_selection[row.selection.value] = row
    best = [row for by_kind in groups.values() for row in _coherent_set(by_kind, priority).values()]
    result: dict[ExternalRef, list[OddsSnapshot]] = {ref: [] for ref in refs.values()}
    for row in sorted(best, key=_display_order):
        ref = refs[row.fixture_id]
        result[ref].append(odds_from_row(row, fixture=ref))
    return result


def _coherent_set(
    by_kind: dict[OddsKind, dict[str, OddsSnapshotRow]], priority: dict[OddsKind, int]
) -> dict[str, OddsSnapshotRow]:
    """The prices of the one kind that best covers the market (see :func:`latest_odds`)."""
    every_selection = {selection for prices in by_kind.values() for selection in prices}
    kind = min(
        by_kind,
        key=lambda candidate: (
            len(every_selection) - len(by_kind[candidate]),
            priority[candidate],
        ),
    )
    return by_kind[kind]


async def odds_history(
    session: AsyncSession,
    fixture: ExternalRef,
    *,
    kinds: Sequence[OddsKind] = tuple(OddsKind),
    bookmakers: Collection[str] = (),
) -> list[OddsSnapshot]:
    """Every stored price of one fixture, oldest first (line movement, CLV)."""
    rows, _ = await _odds_rows(session, [fixture], kinds, bookmakers, None)
    ordered = sorted(rows, key=lambda row: (row.taken_at, *_display_order(row)))
    return [odds_from_row(row, fixture=fixture) for row in ordered]


async def _odds_rows(
    session: AsyncSession,
    fixtures: Iterable[ExternalRef],
    kinds: Sequence[OddsKind],
    bookmakers: Collection[str],
    taken_before: datetime | None,
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
    if taken_before is not None:
        query = query.where(
            or_(OddsSnapshotRow.kind == OddsKind.CLOSING, OddsSnapshotRow.taken_at < taken_before)
        )
    return list((await session.scalars(query)).all()), refs


def _display_order(row: OddsSnapshotRow) -> tuple[str, str, float, str]:
    return (row.bookmaker, row.market.value, row.line, row.selection.value)
