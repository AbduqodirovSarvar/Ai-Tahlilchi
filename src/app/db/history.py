"""Idempotent persistence of provider data (:class:`~app.domain.HistoryBatch`).

Every write is an upsert on a natural key, so re-importing the same data never creates
duplicates:

- leagues, teams, fixtures: ``source`` + ``external_id``;
- opening and closing prices: one per fixture, bookmaker, market, selection, line and
  kind, updated in place (a corrected kick-off time moves ``taken_at`` instead of adding
  a second "closing" price);
- morning / recalc / prematch prices: a time series, so ``taken_at`` is part of the key.

Status and scores of a fixture are merged by :func:`app.db.fixture_state.merge_outcome`.
Functions run inside the caller's transaction: the caller decides the unit of work (e.g.
one CSV file) and commits or rolls back.
"""

from collections import defaultdict
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from itertools import batched
from operator import itemgetter
from typing import Final

from sqlalchemy import Table, TextClause, and_, bindparam, distinct, func, select, text, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.fixture_state import OUTCOME_COLUMNS, FixtureOutcome, merge_outcome
from app.db.mappers import (
    RowValues,
    fixture_values,
    league_values,
    odds_values,
    team_values,
)
from app.db.schema import (
    SINGLE_MOMENT_KINDS,
    SINGLE_MOMENT_ODDS_SQL,
    Base,
    FixtureRow,
    LeagueRow,
    OddsSnapshotRow,
    TeamRow,
    utcnow,
)
from app.domain import (
    BOOKMAKER_PINNACLE,
    ExternalRef,
    FixtureResult,
    HistoryBatch,
    MarketType,
    OddsKind,
    OddsSnapshot,
)
from app.errors import DataError
from app.logging import get_logger

logger = get_logger(__name__)

# IN-clause lookups: keeps every statement far below SQLite's bound-parameter limit (32766).
CHUNK_SIZE: Final = 500
# executemany writes bind one row at a time, so the limit does not apply; large batches
# matter because every async execute costs a thread round trip (aiosqlite), which
# dominated import time with small batches (10k rows: ~23 s -> see ADR 0002).
WRITE_BATCH_SIZE: Final = 20_000
_MOMENT_ODDS_IDENTITY: Final = ("fixture_id", "bookmaker", "market", "selection", "line", "kind")
_SERIES_ODDS_IDENTITY: Final = (*_MOMENT_ODDS_IDENTITY, "taken_at")

type RefModel = type[LeagueRow] | type[TeamRow] | type[FixtureRow]


@dataclass(frozen=True, slots=True)
class _StoredFixture:
    id: int
    outcome: FixtureOutcome


@dataclass(frozen=True, slots=True)
class UpsertSummary:
    """How many rows of each kind were written (inserted or updated)."""

    leagues: int
    teams: int
    fixtures: int
    results: int
    odds: int
    orphan_records: int


@dataclass(frozen=True, slots=True)
class SeasonCoverage:
    """Stored fixtures of one league season and how many have Pinnacle closing 1X2 odds."""

    source: str
    league: str
    season: int
    fixtures: int
    with_pinnacle_closing: int

    @property
    def pinnacle_closing_share(self) -> float:
        """Share (0..1) of fixtures with a Pinnacle closing 1X2 price."""
        return self.with_pinnacle_closing / self.fixtures if self.fixtures else 0.0


async def upsert_batch(session: AsyncSession, batch: HistoryBatch) -> UpsertSummary:
    """Insert or update every league, team, fixture, result and price of ``batch``.

    Results and odds may also refer to fixtures stored earlier; records whose fixture is
    unknown are logged and skipped (counted in ``orphan_records``).
    """
    league_ids = await _upsert_refs(
        session, LeagueRow, [league_values(league) for league in batch.leagues]
    )
    team_ids = await _upsert_refs(session, TeamRow, [team_values(team) for team in batch.teams])
    results = {result.fixture: result for result in batch.results}
    result_count = len(results)
    fixtures = {fixture.ref: fixture for fixture in batch.fixtures}
    stored = await _stored_fixtures(session, _referenced_fixtures(batch))
    fixture_rows = [
        fixture_values(
            fixture,
            league_id=league_ids[fixture.league.ref],
            home_team_id=team_ids[fixture.home.ref],
            away_team_id=team_ids[fixture.away.ref],
            outcome=merge_outcome(
                _outcome_of(stored.get(ref)), fixture.status, results.pop(ref, None)
            ),
        )
        for ref, fixture in fixtures.items()
    ]
    fixture_ids = {ref: entry.id for ref, entry in stored.items()}
    fixture_ids.update(await _upsert_refs(session, FixtureRow, fixture_rows))

    orphan_results = await _apply_results(session, results.values(), stored)
    odds_written, orphan_odds = await _upsert_odds(session, batch.odds, fixture_ids)
    if orphan_odds or orphan_results:
        logger.warning("orphan_records_skipped", odds=orphan_odds, results=orphan_results)
    return UpsertSummary(
        leagues=len(league_ids),
        teams=len(team_ids),
        fixtures=len(fixture_rows),
        results=result_count - orphan_results,
        odds=odds_written,
        orphan_records=orphan_odds + orphan_results,
    )


async def season_coverage(
    session: AsyncSession, *, source: str | None = None
) -> list[SeasonCoverage]:
    """Fixture counts and Pinnacle closing 1X2 coverage per league and season."""
    with_closing = (
        select(OddsSnapshotRow.fixture_id)
        .where(
            OddsSnapshotRow.bookmaker == BOOKMAKER_PINNACLE,
            OddsSnapshotRow.kind == OddsKind.CLOSING,
            OddsSnapshotRow.market == MarketType.MATCH_RESULT,
        )
        .distinct()
        .subquery()
    )
    query = (
        select(
            FixtureRow.source,
            LeagueRow.external_id,
            FixtureRow.season,
            func.count(distinct(FixtureRow.id)),
            func.count(distinct(with_closing.c.fixture_id)),
        )
        .join(LeagueRow, FixtureRow.league_id == LeagueRow.id)
        .outerjoin(with_closing, with_closing.c.fixture_id == FixtureRow.id)
        .group_by(FixtureRow.source, LeagueRow.external_id, FixtureRow.season)
        .order_by(LeagueRow.external_id, FixtureRow.season)
    )
    if source is not None:
        query = query.where(FixtureRow.source == source)
    rows = await session.execute(query)
    return [
        SeasonCoverage(
            source=row_source,
            league=league,
            season=season,
            fixtures=fixtures,
            with_pinnacle_closing=covered,
        )
        for row_source, league, season, fixtures, covered in rows
    ]


def _referenced_fixtures(batch: HistoryBatch) -> set[ExternalRef]:
    referenced = {fixture.ref for fixture in batch.fixtures}
    referenced.update(snapshot.fixture for snapshot in batch.odds)
    referenced.update(result.fixture for result in batch.results)
    return referenced


def _outcome_of(entry: _StoredFixture | None) -> FixtureOutcome | None:
    return None if entry is None else entry.outcome


async def _stored_fixtures(
    session: AsyncSession, refs: Collection[ExternalRef]
) -> dict[ExternalRef, _StoredFixture]:
    """Id and current status/scores of the stored fixtures among ``refs``."""
    columns = [getattr(FixtureRow, name) for name in OUTCOME_COLUMNS]
    stored: dict[ExternalRef, _StoredFixture] = {}
    for source, chunk in _chunks_by_source(refs):
        rows = await session.execute(
            select(FixtureRow.external_id, FixtureRow.id, *columns).where(
                and_(FixtureRow.source == source, FixtureRow.external_id.in_(chunk))
            )
        )
        for external_id, row_id, *outcome in rows:
            stored[ExternalRef(source, external_id)] = _StoredFixture(
                id=row_id, outcome=FixtureOutcome(*outcome)
            )
    return stored


async def _apply_results(
    session: AsyncSession,
    results: Iterable[FixtureResult],
    stored: Mapping[ExternalRef, _StoredFixture],
) -> int:
    """Store results of fixtures that were not part of the batch; return how many were orphans."""
    params: list[RowValues] = []
    orphans = 0
    for result in results:
        entry = stored.get(result.fixture)
        if entry is None:
            orphans += 1
            continue
        merged = merge_outcome(entry.outcome, entry.outcome.status, result)
        values = {f"new_{name}": value for name, value in merged.values().items()}
        params.append({"target_id": entry.id, **values})
    if params:
        table = _table_of(FixtureRow)
        statement = (
            update(table)
            .where(table.c.id == bindparam("target_id"))
            .values(
                updated_at=utcnow(),
                **{name: bindparam(f"new_{name}") for name in OUTCOME_COLUMNS},
            )
        )
        await session.execute(statement, params)
    return orphans


async def _upsert_odds(
    session: AsyncSession, snapshots: Sequence[OddsSnapshot], fixture_ids: Mapping[ExternalRef, int]
) -> tuple[int, int]:
    """Upsert prices of known fixtures; return ``(rows written, orphan snapshots)``."""
    linked = [snapshot for snapshot in snapshots if snapshot.fixture in fixture_ids]
    rows = [odds_values(snap, fixture_id=fixture_ids[snap.fixture]) for snap in linked]
    moments = _unique_by(
        [row for row in rows if row["kind"] in SINGLE_MOMENT_KINDS], _MOMENT_ODDS_IDENTITY
    )
    series = _unique_by(
        [row for row in rows if row["kind"] not in SINGLE_MOMENT_KINDS], _SERIES_ODDS_IDENTITY
    )
    table = _table_of(OddsSnapshotRow)
    await _upsert(
        session,
        table,
        moments,
        _UpsertPlan(
            conflict_columns=_MOMENT_ODDS_IDENTITY,
            update_columns=("odds", "taken_at"),
            index_where=text(SINGLE_MOMENT_ODDS_SQL),
        ),
    )
    await _upsert(
        session,
        table,
        series,
        _UpsertPlan(conflict_columns=_SERIES_ODDS_IDENTITY, update_columns=("odds",)),
    )
    return len(moments) + len(series), len(snapshots) - len(linked)


async def _upsert_refs(
    session: AsyncSession, model: RefModel, rows: Sequence[RowValues]
) -> dict[ExternalRef, int]:
    """Upsert rows keyed by (source, external_id) and return their ids."""
    if not rows:
        return {}
    table = _table_of(model)
    plan = _UpsertPlan(
        conflict_columns=("source", "external_id"),
        update_columns=tuple(name for name in rows[0] if name not in {"source", "external_id"}),
    )
    await _upsert(session, table, rows, plan)
    refs = {ExternalRef(str(row["source"]), str(row["external_id"])) for row in rows}
    ids = await _ids_by_ref(session, model, refs)
    if len(ids) != len(refs):
        msg = f"{table.name}: {len(refs) - len(ids)} upserted row(s) could not be read back"
        raise DataError(msg)
    return ids


async def _ids_by_ref(
    session: AsyncSession, model: RefModel, refs: Collection[ExternalRef]
) -> dict[ExternalRef, int]:
    """Ids of stored rows for ``refs`` (missing refs are simply absent)."""
    ids: dict[ExternalRef, int] = {}
    for source, chunk in _chunks_by_source(refs):
        found = await session.execute(
            select(model.external_id, model.id).where(
                and_(model.source == source, model.external_id.in_(chunk))
            )
        )
        ids.update({ExternalRef(source, external_id): row_id for external_id, row_id in found})
    return ids


def _chunks_by_source(refs: Collection[ExternalRef]) -> Iterator[tuple[str, tuple[str, ...]]]:
    """``(source, external ids)`` groups small enough for one ``IN`` clause."""
    by_source: defaultdict[str, list[str]] = defaultdict(list)
    for ref in refs:
        by_source[ref.source].append(ref.external_id)
    for source, external_ids in by_source.items():
        for chunk in batched(external_ids, CHUNK_SIZE, strict=False):
            yield source, chunk


@dataclass(frozen=True, slots=True)
class _UpsertPlan:
    """Natural key to conflict on and how to merge the other columns."""

    conflict_columns: tuple[str, ...]
    update_columns: tuple[str, ...]
    index_where: TextClause | None = None  # the conflict target is a partial index


async def _upsert(
    session: AsyncSession, table: Table, rows: Sequence[RowValues], plan: _UpsertPlan
) -> None:
    """INSERT ... ON CONFLICT DO UPDATE for SQLite and PostgreSQL, in chunks."""
    if not rows:
        return
    insert = _dialect_insert(session, table)
    assignments: dict[str, object] = {name: insert.excluded[name] for name in plan.update_columns}
    if "updated_at" in table.c:
        assignments["updated_at"] = utcnow()
    statement = insert.on_conflict_do_update(
        index_elements=list(plan.conflict_columns),
        index_where=plan.index_where,
        set_=assignments,
    )
    for chunk in batched(rows, WRITE_BATCH_SIZE, strict=False):
        await session.execute(statement, list(chunk))


def _unique_by(rows: Sequence[RowValues], key: Sequence[str]) -> list[RowValues]:
    """Drop rows repeating an earlier row's key (last one wins).

    One multi-row upsert statement may not touch the same row twice (PostgreSQL rejects it).
    """
    key_of = itemgetter(*key)  # C-level key extraction: this runs once per price row
    unique = {key_of(row): row for row in rows}
    return list(unique.values())


def _table_of(model: type[Base]) -> Table:
    table = model.__table__
    if not isinstance(table, Table):  # pragma: no cover - every model here maps one table
        msg = f"{model.__name__} is not mapped to a single table"
        raise TypeError(msg)
    return table


def _dialect_insert(session: AsyncSession, table: Table) -> sqlite.Insert | postgresql.Insert:
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        return sqlite.insert(table)
    if dialect == "postgresql":
        return postgresql.insert(table)
    msg = f"upsert is not implemented for the {dialect!r} dialect"
    raise DataError(msg)
