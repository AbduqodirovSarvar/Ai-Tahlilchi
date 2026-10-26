"""Read-only queries behind the database browser (what is stored, per league/fixture/team).

Only provider data and reference data are browsable here: leagues, teams, fixtures and price
snapshots. The per-user tables are exposed solely as counts for the asking user
(:func:`table_stats`); nothing in this module can return another user's signals or bets.
Every function is a plain read (no writes, no commits) and returns frozen DTOs, never rows.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import ColumnElement, Select, case, exists, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db.schema import (
    FixtureRow,
    LeagueRow,
    OddsSnapshotRow,
    SignalRow,
    TeamRow,
    UserBetRow,
)
from app.domain import FixtureStatus, MarketType, OddsKind, Selection, Sport, normalize_name

SHARED_TABLES: Final = (
    ("leagues", LeagueRow.id, LeagueRow.updated_at),
    ("teams", TeamRow.id, TeamRow.updated_at),
    ("fixtures", FixtureRow.id, FixtureRow.updated_at),
    ("odds_snapshots", OddsSnapshotRow.id, OddsSnapshotRow.taken_at),
)
"""``(table, a column to count, the column that says when a row was last written)``."""


@dataclass(frozen=True, slots=True)
class TableStat:
    """Rows of one table and when the newest one was written."""

    name: str
    rows: int
    last_updated_at: datetime | None
    own: bool = False


@dataclass(frozen=True, slots=True)
class KickoffSpan:
    """The time range of the stored fixtures and how many have not started."""

    earliest: datetime | None
    latest: datetime | None
    upcoming: int


@dataclass(frozen=True, slots=True)
class LeagueSummary:
    """A stored league with the fixtures the database holds for it."""

    id: int
    sport: Sport
    name: str
    country: str | None
    source: str
    fixtures: int
    upcoming: int
    first_kickoff: datetime | None
    last_kickoff: datetime | None


@dataclass(frozen=True, slots=True, kw_only=True)
class FixtureFilter:
    """Which stored fixtures to list (``start`` inclusive, ``end`` exclusive, both UTC)."""

    start: datetime | None = None
    end: datetime | None = None
    league_ids: Collection[int] = ()
    team_ids: Collection[int] = ()
    statuses: Collection[FixtureStatus] = ()
    with_odds: bool | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredFixtureRow:
    """A fixture with its league, teams, result and whether any price is stored for it."""

    id: int
    sport: Sport
    source: str
    season: int
    league_id: int
    league: str
    country: str | None
    home_team_id: int
    home_team: str
    away_team_id: int
    away_team: str
    kickoff: datetime
    status: FixtureStatus
    home_score: int | None
    away_score: int | None
    home_ht_score: int | None
    away_ht_score: int | None
    home_xg: float | None
    away_xg: float | None
    has_odds: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredOddsRow:
    """One stored price snapshot."""

    bookmaker: str
    market: MarketType
    selection: Selection
    line: float
    odds: float
    kind: OddsKind
    taken_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class TeamRowView:
    """A stored team with the number of fixtures it appears in."""

    id: int
    sport: Sport
    name: str
    country: str | None
    source: str
    fixtures: int


async def table_stats(session: AsyncSession, user_id: int) -> list[TableStat]:
    """Row counts: the shared tables whole, ``signals`` and ``user_bets`` for ``user_id`` only."""
    stats = []
    for name, counted, stamped in SHARED_TABLES:
        row = (await session.execute(select(func.count(counted), func.max(stamped)))).one()
        stats.append(TableStat(name, row[0], row[1]))
    signals = await session.execute(
        select(func.count(SignalRow.id), func.max(SignalRow.updated_at)).where(
            SignalRow.user_id == user_id
        )
    )
    count, stamp = signals.one()
    stats.append(TableStat("signals", count, stamp, own=True))
    bets = await session.execute(
        select(func.count(UserBetRow.id), func.max(UserBetRow.placed_at)).where(
            UserBetRow.user_id == user_id
        )
    )
    count, stamp = bets.one()
    stats.append(TableStat("user_bets", count, stamp, own=True))
    return stats


async def database_size(session: AsyncSession) -> int:
    """Size of the SQLite database in bytes (pages x page size; no file path needed)."""
    pages = await session.scalar(text("PRAGMA page_count")) or 0
    page_size = await session.scalar(text("PRAGMA page_size")) or 0
    return int(pages) * int(page_size)


async def kickoff_span(session: AsyncSession, now: datetime) -> KickoffSpan:
    """Earliest and latest stored kick-off and the number of fixtures after ``now``."""
    row = (
        await session.execute(
            select(
                func.min(FixtureRow.kickoff_at),
                func.max(FixtureRow.kickoff_at),
                func.coalesce(func.sum(case((FixtureRow.kickoff_at >= now, 1), else_=0)), 0),
            )
        )
    ).one()
    return KickoffSpan(row[0], row[1], int(row[2]))


async def league_summaries(
    session: AsyncSession, now: datetime, *, sport: Sport | None = None
) -> list[LeagueSummary]:
    """Every stored league (also one without fixtures yet) by name, with its fixture span."""
    query = (
        select(
            LeagueRow,
            func.count(FixtureRow.id),
            func.coalesce(func.sum(case((FixtureRow.kickoff_at >= now, 1), else_=0)), 0),
            func.min(FixtureRow.kickoff_at),
            func.max(FixtureRow.kickoff_at),
        )
        .outerjoin(FixtureRow, FixtureRow.league_id == LeagueRow.id)
        .group_by(LeagueRow.id)
        .order_by(LeagueRow.name, LeagueRow.id)
    )
    if sport is not None:
        query = query.where(LeagueRow.sport == sport)
    rows = await session.execute(query)
    return [
        LeagueSummary(
            id=league.id,
            sport=league.sport,
            name=league.name,
            country=league.country,
            source=league.source,
            fixtures=fixtures,
            upcoming=int(upcoming),
            first_kickoff=first,
            last_kickoff=last,
        )
        for league, fixtures, upcoming, first, last in rows
    ]


def _fixture_conditions(selected: FixtureFilter) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    if selected.start is not None:
        conditions.append(FixtureRow.kickoff_at >= selected.start)
    if selected.end is not None:
        conditions.append(FixtureRow.kickoff_at < selected.end)
    if selected.league_ids:
        conditions.append(FixtureRow.league_id.in_(list(selected.league_ids)))
    if selected.team_ids:
        teams = list(selected.team_ids)
        conditions.append(
            or_(FixtureRow.home_team_id.in_(teams), FixtureRow.away_team_id.in_(teams))
        )
    if selected.statuses:
        conditions.append(FixtureRow.status.in_(list(selected.statuses)))
    if selected.with_odds is not None:
        conditions.append(_has_odds().is_(selected.with_odds))
    return conditions


def _has_odds() -> ColumnElement[bool]:
    return exists().where(OddsSnapshotRow.fixture_id == FixtureRow.id).correlate(FixtureRow)


def _fixture_select() -> Select[FixtureRow, bool]:
    return select(FixtureRow, _has_odds()).options(
        joinedload(FixtureRow.league),
        joinedload(FixtureRow.home_team),
        joinedload(FixtureRow.away_team),
    )


def _fixture_row(row: FixtureRow, has_odds: bool) -> StoredFixtureRow:
    return StoredFixtureRow(
        id=row.id,
        sport=row.sport,
        source=row.source,
        season=row.season,
        league_id=row.league_id,
        league=row.league.name,
        country=row.league.country,
        home_team_id=row.home_team_id,
        home_team=row.home_team.name,
        away_team_id=row.away_team_id,
        away_team=row.away_team.name,
        kickoff=row.kickoff_at,
        status=row.status,
        home_score=row.home_score,
        away_score=row.away_score,
        home_ht_score=row.home_ht_score,
        away_ht_score=row.away_ht_score,
        home_xg=row.home_xg,
        away_xg=row.away_xg,
        has_odds=bool(has_odds),
    )


async def browse_fixtures(
    session: AsyncSession, selected: FixtureFilter, *, offset: int, limit: int
) -> tuple[list[StoredFixtureRow], int]:
    """One page of the fixtures matching ``selected`` (earliest first) and the total count."""
    conditions = _fixture_conditions(selected)
    total = await session.scalar(select(func.count(FixtureRow.id)).where(*conditions)) or 0
    rows = await session.execute(
        _fixture_select()
        .where(*conditions)
        .order_by(FixtureRow.kickoff_at, FixtureRow.id)
        .offset(offset)
        .limit(limit)
    )
    return [_fixture_row(row, has_odds) for row, has_odds in rows.unique()], total


async def stored_fixture(session: AsyncSession, fixture_id: int) -> StoredFixtureRow | None:
    """One stored fixture by database id."""
    result = await session.execute(_fixture_select().where(FixtureRow.id == fixture_id))
    found = result.unique().first()
    return None if found is None else _fixture_row(found[0], found[1])


async def stored_odds(  # noqa: PLR0913 - one filter per facet of the odds list
    session: AsyncSession,
    fixture_id: int,
    *,
    bookmaker: str | None = None,
    market: MarketType | None = None,
    offset: int = 0,
    limit: int | None = None,
) -> tuple[list[StoredOddsRow], int]:
    """Price snapshots of one fixture, newest first, with the total that match the filters."""
    conditions: list[ColumnElement[bool]] = [OddsSnapshotRow.fixture_id == fixture_id]
    if bookmaker is not None:
        conditions.append(OddsSnapshotRow.bookmaker == bookmaker)
    if market is not None:
        conditions.append(OddsSnapshotRow.market == market)
    total = await session.scalar(select(func.count(OddsSnapshotRow.id)).where(*conditions)) or 0
    rows = await session.scalars(
        select(OddsSnapshotRow)
        .where(*conditions)
        .order_by(
            OddsSnapshotRow.taken_at.desc(),
            OddsSnapshotRow.bookmaker,
            OddsSnapshotRow.market,
            OddsSnapshotRow.line,
            OddsSnapshotRow.selection,
            OddsSnapshotRow.id,
        )
        .offset(offset)
        .limit(limit)
    )
    return [
        StoredOddsRow(
            bookmaker=row.bookmaker,
            market=row.market,
            selection=row.selection,
            line=row.line,
            odds=row.odds,
            kind=row.kind,
            taken_at=row.taken_at,
        )
        for row in rows
    ], total


def _team_conditions(
    text_filter: str, sport: Sport | None, league_ids: Collection[int]
) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    needle = normalize_name(text_filter)
    if needle:
        conditions.append(TeamRow.normalized_name.contains(needle, autoescape=True))
    if sport is not None:
        conditions.append(TeamRow.sport == sport)
    if league_ids:
        leagues = list(league_ids)
        home = select(FixtureRow.home_team_id).where(FixtureRow.league_id.in_(leagues))
        away = select(FixtureRow.away_team_id).where(FixtureRow.league_id.in_(leagues))
        conditions.append(or_(TeamRow.id.in_(home), TeamRow.id.in_(away)))
    return conditions


async def browse_teams(  # noqa: PLR0913 - one filter per facet of the team list
    session: AsyncSession,
    *,
    text_filter: str = "",
    sport: Sport | None = None,
    league_ids: Collection[int] = (),
    offset: int = 0,
    limit: int | None = None,
) -> tuple[list[TeamRowView], int]:
    """One page of stored teams by name and the total count, each with its fixture count."""
    conditions = _team_conditions(text_filter, sport, league_ids)
    total = await session.scalar(select(func.count(TeamRow.id)).where(*conditions)) or 0
    played = (
        select(func.count(FixtureRow.id))
        .where(or_(FixtureRow.home_team_id == TeamRow.id, FixtureRow.away_team_id == TeamRow.id))
        .correlate(TeamRow)
        .scalar_subquery()
    )
    rows = await session.execute(
        select(TeamRow, played)
        .where(*conditions)
        .order_by(TeamRow.name, TeamRow.id)
        .offset(offset)
        .limit(limit)
    )
    return [
        TeamRowView(
            id=team.id,
            sport=team.sport,
            name=team.name,
            country=team.country,
            source=team.source,
            fixtures=int(count or 0),
        )
        for team, count in rows
    ], total
