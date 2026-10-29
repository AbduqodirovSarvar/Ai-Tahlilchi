"""Translation between domain objects and ORM rows (the only place that knows both).

``*_values`` functions build column dicts for bulk upserts; ``*_from_row`` functions
rebuild validated domain objects (a corrupt row raises
:class:`~app.domain.DomainValidationError` instead of leaking bad data upwards).
"""

from app.db.fixture_state import FixtureOutcome
from app.db.schema import NO_LINE, FixtureRow, LeagueRow, OddsSnapshotRow, TeamRow
from app.domain import (
    LINE_MARKETS,
    ExternalRef,
    Fixture,
    FixtureResult,
    League,
    Market,
    MarketType,
    OddsSnapshot,
    ResultStatus,
    Team,
)

type RowValues = dict[str, object]

_RESULT_STATUSES = frozenset(status.value for status in ResultStatus)


def line_to_db(market: Market) -> float:
    """Stored line: the market line, or ``NO_LINE`` for markets without one."""
    return NO_LINE if market.line is None else market.line


def market_from_db(market_type: MarketType, line: float) -> Market:
    """Rebuild a market from its stored type and line."""
    return Market(market_type, line if market_type in LINE_MARKETS else None)


def league_values(league: League) -> RowValues:
    """Columns of ``leagues`` for ``league``."""
    return {
        "sport": league.sport,
        "source": league.ref.source,
        "external_id": league.ref.external_id,
        "name": league.name,
        "country": league.country,
    }


def team_values(team: Team) -> RowValues:
    """Columns of ``teams`` for ``team`` (keeps the source spelling and a lookup key)."""
    return {
        "sport": team.sport,
        "source": team.ref.source,
        "external_id": team.ref.external_id,
        "name": team.name,
        "normalized_name": team.normalized_name,
        "country": team.country,
    }


def fixture_values(
    fixture: Fixture,
    *,
    league_id: int,
    home_team_id: int,
    away_team_id: int,
    outcome: FixtureOutcome,
) -> RowValues:
    """Columns of ``fixtures``; status and scores come from the merged ``outcome``."""
    return {
        "sport": fixture.sport,
        "source": fixture.ref.source,
        "external_id": fixture.ref.external_id,
        "league_id": league_id,
        "season": fixture.season,
        "kickoff_at": fixture.kickoff,
        "home_team_id": home_team_id,
        "away_team_id": away_team_id,
        **outcome.values(),
    }


def odds_values(snapshot: OddsSnapshot, *, fixture_id: int) -> RowValues:
    """Columns of ``odds_snapshots`` for ``snapshot``."""
    return {
        "fixture_id": fixture_id,
        "bookmaker": snapshot.bookmaker,
        "market": snapshot.market.type,
        "selection": snapshot.selection,
        "line": line_to_db(snapshot.market),
        "odds": snapshot.odds,
        "kind": snapshot.kind,
        "taken_at": snapshot.taken_at,
    }


def league_from_row(row: LeagueRow) -> League:
    """Domain league of a ``leagues`` row."""
    return League(
        ref=ExternalRef(row.source, row.external_id),
        sport=row.sport,
        name=row.name,
        country=row.country,
    )


def team_from_row(row: TeamRow) -> Team:
    """Domain team of a ``teams`` row."""
    return Team(
        ref=ExternalRef(row.source, row.external_id),
        sport=row.sport,
        name=row.name,
        country=row.country,
    )


def fixture_from_row(row: FixtureRow) -> Fixture:
    """Domain fixture; ``league``, ``home_team`` and ``away_team`` must be loaded eagerly."""
    return Fixture(
        ref=ExternalRef(row.source, row.external_id),
        sport=row.sport,
        league=league_from_row(row.league),
        season=row.season,
        kickoff=row.kickoff_at,
        home=team_from_row(row.home_team),
        away=team_from_row(row.away_team),
        status=row.status,
    )


def result_from_row(row: FixtureRow) -> FixtureResult | None:
    """The fixture's result, or ``None`` while it is scheduled or live."""
    if row.status.value not in _RESULT_STATUSES:
        return None
    return FixtureResult(
        fixture=ExternalRef(row.source, row.external_id),
        status=ResultStatus(row.status.value),
        home_score=row.home_score,
        away_score=row.away_score,
        home_ht_score=row.home_ht_score,
        away_ht_score=row.away_ht_score,
        home_xg=row.home_xg,
        away_xg=row.away_xg,
        home_yellow=row.home_yellow,
        away_yellow=row.away_yellow,
        home_red=row.home_red,
        away_red=row.away_red,
        referee=row.referee,
    )


def odds_from_row(row: OddsSnapshotRow, *, fixture: ExternalRef) -> OddsSnapshot:
    """Domain snapshot of an ``odds_snapshots`` row belonging to ``fixture``."""
    return OddsSnapshot(
        fixture=fixture,
        bookmaker=row.bookmaker,
        market=market_from_db(row.market, row.line),
        selection=row.selection,
        odds=row.odds,
        taken_at=row.taken_at,
        kind=row.kind,
    )
