"""Idempotent persistence of provider data (:class:`~app.domain.HistoryBatch`).

Every write is an upsert on the natural key (``source`` + ``external_id`` for leagues,
teams and fixtures; the full price identity for odds), so re-importing the same data never
creates duplicates. Functions run inside the caller's transaction: the caller decides the
unit of work (e.g. one CSV file) and commits or rolls back.
"""

from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import batched
from typing import Final

from sqlalchemy import Table, and_, bindparam, distinct, func, select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.mappers import (
    RowValues,
    fixture_values,
    league_values,
    odds_values,
    result_values,
    team_values,
)
from app.db.schema import Base, FixtureRow, LeagueRow, OddsSnapshotRow, TeamRow, utcnow
from app.domain import (
    BOOKMAKER_PINNACLE,
    ExternalRef,
    FixtureResult,
    HistoryBatch,
    MarketType,
    OddsKind,
)
from app.errors import DataError
from app.logging import get_logger

logger = get_logger(__name__)

# Keeps every statement far below SQLite's bound-parameter limit (32766).
CHUNK_SIZE: Final = 500
_FIXTURE_UPDATE_COLUMNS: Final = (
    "sport",
    "league_id",
    "season",
    "kickoff_at",
    "home_team_id",
    "away_team_id",
    "status",
)
# A refresh without a result (e.g. fixture list) must not erase a stored score.
_KEEP_WHEN_NULL_COLUMNS: Final = tuple(result_values(None))
_ODDS_IDENTITY: Final = (
    "fixture_id",
    "bookmaker",
    "market",
    "selection",
    "line",
    "kind",
    "taken_at",
)

type RefModel = type[LeagueRow] | type[TeamRow] | type[FixtureRow]


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
    fixture_rows = [
        fixture_values(
            fixture,
            league_id=league_ids[fixture.league.ref],
            home_team_id=team_ids[fixture.home.ref],
            away_team_id=team_ids[fixture.away.ref],
            result=results.pop(fixture.ref, None),
        )
        for fixture in fixtures.values()
    ]
    fixture_ids = await _upsert_refs(session, FixtureRow, fixture_rows)
    fixture_ids.update(await _ids_by_ref(session, FixtureRow, _unknown_refs(batch, fixture_ids)))

    orphan_results = await _apply_results(session, results.values(), fixture_ids)
    linked_odds = [snapshot for snapshot in batch.odds if snapshot.fixture in fixture_ids]
    odds_rows = _unique_by(
        [odds_values(snap, fixture_id=fixture_ids[snap.fixture]) for snap in linked_odds],
        _ODDS_IDENTITY,
    )
    await _upsert(
        session,
        _table_of(OddsSnapshotRow),
        odds_rows,
        _UpsertPlan(conflict_columns=_ODDS_IDENTITY, update_columns=("odds",)),
    )
    orphan_odds = len(batch.odds) - len(linked_odds)
    if orphan_odds or orphan_results:
        logger.warning("orphan_records_skipped", odds=orphan_odds, results=orphan_results)
    return UpsertSummary(
        leagues=len(league_ids),
        teams=len(team_ids),
        fixtures=len(fixture_rows),
        results=result_count - orphan_results,
        odds=len(odds_rows),
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


def _unknown_refs(batch: HistoryBatch, known: Mapping[ExternalRef, int]) -> set[ExternalRef]:
    referenced = {snapshot.fixture for snapshot in batch.odds}
    referenced.update(result.fixture for result in batch.results)
    return referenced - known.keys()


async def _apply_results(
    session: AsyncSession, results: Iterable[FixtureResult], fixture_ids: Mapping[ExternalRef, int]
) -> int:
    """Store results of fixtures that were not part of the batch; return how many were orphans."""
    params: list[RowValues] = []
    orphans = 0
    for result in results:
        fixture_id = fixture_ids.get(result.fixture)
        if fixture_id is None:
            orphans += 1
            continue
        values = {f"new_{name}": value for name, value in result_values(result).items()}
        params.append({"target_id": fixture_id, "new_status": result.status.value, **values})
    if params:
        table = _table_of(FixtureRow)
        statement = (
            update(table)
            .where(table.c.id == bindparam("target_id"))
            .values(
                status=bindparam("new_status"),
                updated_at=utcnow(),
                **{name: bindparam(f"new_{name}") for name in _KEEP_WHEN_NULL_COLUMNS},
            )
        )
        await session.execute(statement, params)
    return orphans


async def _upsert_refs(
    session: AsyncSession, model: RefModel, rows: Sequence[RowValues]
) -> dict[ExternalRef, int]:
    """Upsert rows keyed by (source, external_id) and return their ids."""
    if not rows:
        return {}
    table = _table_of(model)
    plan = _UpsertPlan(
        conflict_columns=("source", "external_id"),
        update_columns=tuple(
            name
            for name in rows[0]
            if name not in {"source", "external_id", *_KEEP_WHEN_NULL_COLUMNS}
        ),
        keep_when_null=tuple(name for name in _KEEP_WHEN_NULL_COLUMNS if name in rows[0]),
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
    by_source: defaultdict[str, list[str]] = defaultdict(list)
    for ref in refs:
        by_source[ref.source].append(ref.external_id)
    ids: dict[ExternalRef, int] = {}
    for source, external_ids in by_source.items():
        for chunk in batched(external_ids, CHUNK_SIZE, strict=False):
            found = await session.execute(
                select(model.external_id, model.id).where(
                    and_(model.source == source, model.external_id.in_(chunk))
                )
            )
            ids.update({ExternalRef(source, external_id): row_id for external_id, row_id in found})
    return ids


@dataclass(frozen=True, slots=True)
class _UpsertPlan:
    """Natural key to conflict on and how to merge the other columns."""

    conflict_columns: tuple[str, ...]
    update_columns: tuple[str, ...]
    keep_when_null: tuple[str, ...] = ()


async def _upsert(
    session: AsyncSession, table: Table, rows: Sequence[RowValues], plan: _UpsertPlan
) -> None:
    """INSERT ... ON CONFLICT DO UPDATE for SQLite and PostgreSQL, in chunks."""
    if not rows:
        return
    insert = _dialect_insert(session, table)
    assignments: dict[str, object] = {name: insert.excluded[name] for name in plan.update_columns}
    assignments.update(
        {name: func.coalesce(insert.excluded[name], table.c[name]) for name in plan.keep_when_null}
    )
    if "updated_at" in table.c:
        assignments["updated_at"] = utcnow()
    statement = insert.on_conflict_do_update(
        index_elements=list(plan.conflict_columns), set_=assignments
    )
    for chunk in batched(rows, CHUNK_SIZE, strict=False):
        await session.execute(statement, list(chunk))


def _unique_by(rows: Sequence[RowValues], key: Sequence[str]) -> list[RowValues]:
    """Drop rows repeating an earlier row's key (last one wins).

    One multi-row upsert statement may not touch the same row twice (PostgreSQL rejects it).
    """
    unique = {tuple(row[name] for name in key): row for row in rows}
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
