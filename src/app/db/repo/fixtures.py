"""Fixture queries and writes of live provider data.

Reads join league and both teams in the same statement (no lazy loads, no N+1). Writes
reuse :func:`app.db.history.upsert_batch`, so live data follows exactly the same upsert
and status/score merge rules (:func:`app.db.fixture_state.merge_outcome`) as imports.
"""

from collections.abc import Collection, Iterable, Sequence
from datetime import datetime
from typing import Final

from sqlalchemy import ColumnElement, Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db.history import UpsertSummary, upsert_batch
from app.db.mappers import fixture_from_row, result_from_row
from app.db.repo.records import FixtureRecord, Page
from app.db.schema import FixtureRow, LeagueRow, TeamRow
from app.domain import (
    ExternalRef,
    Fixture,
    FixtureResult,
    FixtureStatus,
    HistoryBatch,
    OddsSnapshot,
    Sport,
)

_EAGER: Final = (
    joinedload(FixtureRow.league),
    joinedload(FixtureRow.home_team),
    joinedload(FixtureRow.away_team),
)


def to_record(row: FixtureRow) -> FixtureRecord:
    """DTO of a fixture row loaded with league and teams."""
    return FixtureRecord(
        id=row.id,
        fixture=fixture_from_row(row),
        result=result_from_row(row),
        home_xg=row.home_xg,
        away_xg=row.away_xg,
    )


def _ref_filter(
    model: type[LeagueRow] | type[TeamRow], refs: Collection[ExternalRef]
) -> ColumnElement[bool]:
    return or_(
        *(and_(model.source == ref.source, model.external_id == ref.external_id) for ref in refs)
    )


def _ids_of(model: type[LeagueRow] | type[TeamRow], refs: Collection[ExternalRef]) -> Select[int]:
    return select(model.id).where(_ref_filter(model, refs))


async def list_fixtures(  # noqa: PLR0913 - mirrors the service's fixture filters
    session: AsyncSession,
    *,
    start: datetime,
    end: datetime,
    sport: Sport | None = None,
    leagues: Collection[ExternalRef] = (),
    teams: Collection[ExternalRef] = (),
    statuses: Collection[FixtureStatus] = (),
    offset: int = 0,
    limit: int | None = None,
) -> Page[FixtureRecord]:
    """Fixtures kicking off in ``[start, end)`` (UTC), by kick-off then id.

    ``leagues`` and ``teams`` narrow the scope; when both are given a fixture matches
    either (union, as in :class:`~app.sports.base.FixtureQuery`).
    """
    conditions = [FixtureRow.kickoff_at >= start, FixtureRow.kickoff_at < end]
    if sport is not None:
        conditions.append(FixtureRow.sport == sport)
    if statuses:
        conditions.append(FixtureRow.status.in_(list(statuses)))
    scope = []
    if leagues:
        scope.append(FixtureRow.league_id.in_(_ids_of(LeagueRow, leagues)))
    if teams:
        team_ids = _ids_of(TeamRow, teams)
        scope.extend([FixtureRow.home_team_id.in_(team_ids), FixtureRow.away_team_id.in_(team_ids)])
    if scope:
        conditions.append(or_(*scope))
    total = await session.scalar(select(func.count(FixtureRow.id)).where(*conditions)) or 0
    query = (
        select(FixtureRow)
        .options(*_EAGER)
        .where(*conditions)
        .order_by(FixtureRow.kickoff_at, FixtureRow.id)
        .offset(offset)
        .limit(limit)
    )
    rows = (await session.scalars(query)).all()
    return Page(tuple(to_record(row) for row in rows), total, offset, limit)


async def get_fixtures(
    session: AsyncSession, refs: Iterable[ExternalRef]
) -> dict[ExternalRef, FixtureRecord]:
    """Stored fixtures among ``refs`` (unknown refs are absent)."""
    wanted = set(refs)
    if not wanted:
        return {}
    rows = await session.scalars(
        select(FixtureRow).options(*_EAGER).where(_fixture_ref_filter(wanted))
    )
    return {record.fixture.ref: record for record in map(to_record, rows)}


async def get_fixture_by_id(session: AsyncSession, fixture_id: int) -> FixtureRecord | None:
    """A stored fixture by database id."""
    row = await session.scalar(
        select(FixtureRow).options(*_EAGER).where(FixtureRow.id == fixture_id)
    )
    return None if row is None else to_record(row)


def _fixture_ref_filter(refs: Collection[ExternalRef]) -> ColumnElement[bool]:
    by_source: dict[str, list[str]] = {}
    for ref in refs:
        by_source.setdefault(ref.source, []).append(ref.external_id)
    return or_(
        *(
            and_(FixtureRow.source == source, FixtureRow.external_id.in_(ids))
            for source, ids in by_source.items()
        )
    )


async def finished_fixtures(
    session: AsyncSession,
    *,
    leagues: Collection[ExternalRef],
    start: datetime,
    end: datetime,
    preferred_sources: Sequence[str] = (),
) -> list[FixtureRecord]:
    """Finished fixtures with scores in ``[start, end)`` for model fitting, oldest first.

    The same match may be stored twice when history (football-data) and live data
    (API-Football) both cover it and teams/leagues share one identity
    (:class:`app.sports.team_matching.IdentityMap`). Rows are therefore de-duplicated on
    ``(league, season, home, away)`` — a pairing is played once per league season — keeping
    the row whose source comes first in ``preferred_sources`` (others after, by id).
    """
    if not leagues:
        return []
    rows = await session.scalars(
        select(FixtureRow)
        .options(*_EAGER)
        .where(
            FixtureRow.league_id.in_(_ids_of(LeagueRow, leagues)),
            FixtureRow.status == FixtureStatus.FINISHED,
            FixtureRow.home_score.is_not(None),
            FixtureRow.kickoff_at >= start,
            FixtureRow.kickoff_at < end,
        )
        .order_by(FixtureRow.kickoff_at, FixtureRow.id)
    )
    rank = {source: index for index, source in enumerate(preferred_sources)}
    best: dict[tuple[int, int, int, int], FixtureRow] = {}
    for row in rows:
        key = (row.league_id, row.season, row.home_team_id, row.away_team_id)
        current = best.get(key)
        if current is None or rank.get(row.source, len(rank)) < rank.get(current.source, len(rank)):
            best[key] = row
    chosen = sorted(best.values(), key=lambda row: (row.kickoff_at, row.id))
    return [to_record(row) for row in chosen]


async def upsert_live(
    session: AsyncSession,
    *,
    fixtures: Sequence[Fixture] = (),
    results: Sequence[FixtureResult] = (),
    odds: Sequence[OddsSnapshot] = (),
) -> UpsertSummary:
    """Idempotently store adapter output (fixtures, results, odds) in the caller's transaction.

    Results and odds may refer to fixtures stored earlier; orphans are logged and skipped.
    """
    batch = HistoryBatch(fixtures=tuple(fixtures), results=tuple(results), odds=tuple(odds))
    return await upsert_batch(session, batch)
