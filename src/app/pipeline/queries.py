"""Read queries the pipeline needs that the repository (``app.db.repo``) does not offer yet.

Small, read-only and domain-typed (no ORM rows leave this module). They are listed as a
request to the data-engineer in ADR 0008; once ``app.db.repo`` has equivalents the pipeline
switches to them and this module disappears.
"""

from collections.abc import Collection

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.mappers import league_from_row, team_from_row
from app.db.schema import FixtureRow, LeagueRow, TeamRow, UserRow
from app.domain import ExternalRef, League, Sport, Team


async def list_leagues(session: AsyncSession, sport: Sport) -> list[tuple[int, League]]:
    """Every stored league of ``sport`` with its database id, ordered by name then id."""
    rows = await session.scalars(
        select(LeagueRow).where(LeagueRow.sport == sport).order_by(LeagueRow.name, LeagueRow.id)
    )
    return [(row.id, league_from_row(row)) for row in rows]


async def get_league(session: AsyncSession, league_id: int) -> tuple[int, League] | None:
    """One league by database id."""
    row = await session.get(LeagueRow, league_id)
    return None if row is None else (row.id, league_from_row(row))


async def get_team(session: AsyncSession, team_id: int) -> tuple[int, Team] | None:
    """One team by database id."""
    row = await session.get(TeamRow, team_id)
    return None if row is None else (row.id, team_from_row(row))


async def league_refs(session: AsyncSession, ids: Collection[int]) -> dict[int, ExternalRef]:
    """References of the leagues with these database ids (unknown ids are absent)."""
    if not ids:
        return {}
    rows = await session.execute(
        select(LeagueRow.id, LeagueRow.source, LeagueRow.external_id).where(
            LeagueRow.id.in_(list(ids))
        )
    )
    return {row_id: ExternalRef(source, external) for row_id, source, external in rows}


async def team_refs(session: AsyncSession, ids: Collection[int]) -> dict[int, ExternalRef]:
    """References of the teams with these database ids (unknown ids are absent)."""
    if not ids:
        return {}
    rows = await session.execute(
        select(TeamRow.id, TeamRow.source, TeamRow.external_id).where(TeamRow.id.in_(list(ids)))
    )
    return {row_id: ExternalRef(source, external) for row_id, source, external in rows}


async def league_teams(
    session: AsyncSession, leagues: Collection[ExternalRef], *, source: str
) -> list[Team]:
    """Teams of ``source`` that played in any of ``leagues`` (the history side of matching)."""
    if not leagues:
        return []
    league_ids = select(LeagueRow.id).where(
        or_(
            *(
                (LeagueRow.source == ref.source) & (LeagueRow.external_id == ref.external_id)
                for ref in leagues
            )
        )
    )
    home = select(FixtureRow.home_team_id).where(FixtureRow.league_id.in_(league_ids))
    away = select(FixtureRow.away_team_id).where(FixtureRow.league_id.in_(league_ids))
    rows = await session.scalars(
        select(TeamRow)
        .where(TeamRow.source == source, or_(TeamRow.id.in_(home), TeamRow.id.in_(away)))
        .order_by(TeamRow.id)
    )
    return [team_from_row(row) for row in rows]


async def user_ids(session: AsyncSession) -> list[int]:
    """Every known user id, ascending."""
    return list(await session.scalars(select(UserRow.id).order_by(UserRow.id)))
