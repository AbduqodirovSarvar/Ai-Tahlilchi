"""Real :class:`~app.services.interfaces.DataBrowserService`: what the database holds (v1.11).

Everything except :meth:`RealDataBrowserService.refresh_database` is a plain read of stored
data. Leagues, teams, fixtures and prices are shared provider data and visible to every
user; signals are always filtered to the asking user (a stored fixture's detail shows only
that user's own signals, and the table counts for ``signals``/``user_bets`` are theirs).
"""

from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from app.domain import Sport
from app.logging import get_logger
from app.pipeline import data
from app.pipeline.errors import BudgetExhaustedError, ProviderUnavailableError, RefreshTooSoonError
from app.pipeline.freshness import current_freshness
from app.pipeline.profile import utc_bounds
from app.pipeline.refresh import refresh_manual
from app.services.errors import (
    BudgetExceededError,
    CooldownActiveError,
    NotAuthorizedError,
    NotFoundError,
    ServiceUnavailableError,
)
from app.services.interfaces import (
    MAX_STORED_ODDS_IN_DETAIL,
    BookmakerOdds,
    DatabaseOverview,
    EntityId,
    RefreshReport,
    StoredFixture,
    StoredFixtureDetail,
    StoredFixturePage,
    StoredFixtureRequest,
    StoredLeague,
    StoredOdds,
    StoredOddsPage,
    StoredOddsRequest,
    StoredTable,
    StoredTeam,
    StoredTeamPage,
    StoredTeamRequest,
)
from app.services.real.core import Core, parse_ids, to_view

logger = get_logger(__name__)

_ONE_MICROSECOND: Final = timedelta(microseconds=1)


def _page_count(total: int, size: int) -> int:
    """Pages needed for ``total`` items (0 when there are none)."""
    return -(-total // size)


def _entity(value: int) -> EntityId:
    return str(value)


def _number(entity: str, value: EntityId) -> int:
    """The storage id behind a fixture id; a malformed id is simply unknown."""
    try:
        return int(value)
    except ValueError:
        raise NotFoundError(entity, value) from None


def _stored_fixture(row: data.StoredFixtureRow) -> StoredFixture:
    return StoredFixture(
        fixture_id=_entity(row.id),
        sport=row.sport,
        league_id=_entity(row.league_id),
        league=row.league,
        country=row.country,
        home_team=row.home_team,
        away_team=row.away_team,
        home_team_id=_entity(row.home_team_id),
        away_team_id=_entity(row.away_team_id),
        kickoff=row.kickoff,
        status=row.status,
        home_score=row.home_score,
        away_score=row.away_score,
        has_odds=row.has_odds,
        source=row.source,
    )


def _stored_odds(row: data.StoredOddsRow) -> StoredOdds:
    return StoredOdds(
        bookmaker=row.bookmaker,
        market=row.market,
        selection=row.selection,
        line=row.line,
        odds=row.odds,
        kind=row.kind,
        taken_at=row.taken_at,
    )


def _by_bookmaker(rows: Sequence[data.StoredOddsRow]) -> tuple[BookmakerOdds, ...]:
    """Snapshots grouped by bookmaker (alphabetical), each group oldest first."""
    groups: defaultdict[str, list[StoredOdds]] = defaultdict(list)
    for row in rows:
        groups[row.bookmaker].append(_stored_odds(row))
    return tuple(
        BookmakerOdds(
            bookmaker=name,
            quotes=tuple(
                sorted(
                    groups[name],
                    key=lambda q: (q.taken_at, q.market.value, q.line, q.selection.value),
                )
            ),
        )
        for name in sorted(groups)
    )


class RealDataBrowserService:
    """:class:`~app.services.interfaces.DataBrowserService` over the stored data."""

    def __init__(self, core: Core) -> None:
        """Share ``core`` with the other real services."""
        self._core = core

    async def database_overview(self, user_id: int) -> DatabaseOverview:
        """See :meth:`DataBrowserService.database_overview`."""
        ctx = self._core.ctx
        now = self._core.now()
        async with ctx.sessions() as session:
            stats = await data.table_stats(session, user_id)
            size = await data.database_size(session)
            span = await data.kickoff_span(session, now)
        current = await current_freshness(ctx)
        return DatabaseOverview(
            tables=tuple(
                StoredTable(
                    name=stat.name,
                    rows=stat.rows,
                    last_updated_at=stat.last_updated_at,
                    own=stat.own,
                )
                for stat in stats
            ),
            size_bytes=size,
            last_refresh_at=current.last_refresh_at,
            earliest_kickoff=span.earliest,
            latest_kickoff=span.latest,
            upcoming_fixtures=span.upcoming,
            data_stale=current.stale,
        )

    async def list_stored_leagues(
        self, user_id: int, *, sport: Sport | None = None
    ) -> tuple[StoredLeague, ...]:
        """See :meth:`DataBrowserService.list_stored_leagues`."""
        _ = user_id  # shared reference data: the same for everyone
        async with self._core.ctx.sessions() as session:
            leagues = await data.league_summaries(session, self._core.now(), sport=sport)
        return tuple(
            StoredLeague(
                league_id=_entity(league.id),
                sport=league.sport,
                name=league.name,
                country=league.country,
                source=league.source,
                fixtures=league.fixtures,
                upcoming=league.upcoming,
                first_kickoff=league.first_kickoff,
                last_kickoff=league.last_kickoff,
            )
            for league in leagues
        )

    async def list_stored_fixtures(
        self, user_id: int, request: StoredFixtureRequest
    ) -> StoredFixturePage:
        """See :meth:`DataBrowserService.list_stored_fixtures`."""
        profile = await self._core.profile(user_id)
        start, end = _bounds(profile.zone, request.date_from, request.date_to)
        selected = data.FixtureFilter(
            start=start,
            end=end,
            league_ids=parse_ids(request.league_ids, "league_ids"),
            team_ids=parse_ids(request.team_ids, "team_ids"),
            statuses=request.statuses,
            with_odds=request.with_odds,
        )
        async with self._core.ctx.sessions() as session:
            rows, total = await data.browse_fixtures(
                session,
                selected,
                offset=request.page * request.page_size,
                limit=request.page_size,
            )
        return StoredFixturePage(
            items=tuple(_stored_fixture(row) for row in rows),
            page=request.page,
            page_count=_page_count(total, request.page_size),
            total=total,
        )

    async def get_stored_fixture(self, user_id: int, fixture_id: EntityId) -> StoredFixtureDetail:
        """See :meth:`DataBrowserService.get_stored_fixture`."""
        number = _number("fixture", fixture_id)
        async with self._core.ctx.sessions() as session:
            row = await data.stored_fixture(session, number)
            if row is None:
                raise NotFoundError("fixture", fixture_id)
            odds, total = await data.stored_odds(session, number, limit=MAX_STORED_ODDS_IN_DETAIL)
            # Only this user's signals: the window is the fixture's own kick-off instant.
            signals = await data.list_user_signals(
                session,
                user_id,
                start=row.kickoff,
                end=row.kickoff + _ONE_MICROSECOND,
                include_hidden=True,
                include_retro=True,
            )
        return StoredFixtureDetail(
            fixture=_stored_fixture(row),
            season=row.season,
            home_ht_score=row.home_ht_score,
            away_ht_score=row.away_ht_score,
            home_xg=row.home_xg,
            away_xg=row.away_xg,
            odds=_by_bookmaker(odds),
            odds_total=total,
            truncated=total > len(odds),
            signals=tuple(
                to_view(self._core, record) for record in signals if record.fixture.id == number
            ),
        )

    async def list_stored_teams(self, user_id: int, request: StoredTeamRequest) -> StoredTeamPage:
        """See :meth:`DataBrowserService.list_stored_teams`."""
        _ = user_id  # shared reference data: the same for everyone
        async with self._core.ctx.sessions() as session:
            rows, total = await data.browse_teams(
                session,
                text_filter=request.text,
                sport=request.sport,
                league_ids=parse_ids(request.league_ids, "league_ids"),
                offset=request.page * request.page_size,
                limit=request.page_size,
            )
        return StoredTeamPage(
            items=tuple(
                StoredTeam(
                    team_id=_entity(row.id),
                    sport=row.sport,
                    name=row.name,
                    country=row.country,
                    source=row.source,
                    fixtures=row.fixtures,
                )
                for row in rows
            ),
            page=request.page,
            page_count=_page_count(total, request.page_size),
            total=total,
        )

    async def list_stored_odds(self, user_id: int, request: StoredOddsRequest) -> StoredOddsPage:
        """See :meth:`DataBrowserService.list_stored_odds`."""
        _ = user_id  # shared provider data: the same for everyone
        number = _number("fixture", request.fixture_id)
        async with self._core.ctx.sessions() as session:
            if await data.stored_fixture(session, number) is None:
                raise NotFoundError("fixture", request.fixture_id)
            rows, total = await data.stored_odds(
                session,
                number,
                bookmaker=request.bookmaker,
                market=request.market,
                offset=request.page * request.page_size,
                limit=request.page_size,
            )
        return StoredOddsPage(
            items=tuple(_stored_odds(row) for row in rows),
            page=request.page,
            page_count=_page_count(total, request.page_size),
            total=total,
        )

    async def refresh_database(self, user_id: int) -> RefreshReport:
        """See :meth:`DataBrowserService.refresh_database`."""
        if not self._core.is_admin(user_id):
            raise NotAuthorizedError(user_id, "refresh the database")
        try:
            report = await refresh_manual(self._core.ctx)
        except RefreshTooSoonError as error:
            raise CooldownActiveError(error.retry_after) from error
        except BudgetExhaustedError as error:
            raise BudgetExceededError(error.needed, error.available) from error
        except ProviderUnavailableError as error:
            raise ServiceUnavailableError(error.provider, error.detail) from error
        logger.info("database_refreshed_by_admin", user_id=user_id, requests=report.api_requests)
        return RefreshReport(
            started_at=report.started_at,
            finished_at=report.finished_at,
            fixtures=report.fixtures,
            results=report.results,
            odds=report.odds,
            api_requests=report.api_requests,
            skipped=report.skipped,
        )


def _bounds(
    zone: ZoneInfo, first: date | None, last: date | None
) -> tuple[datetime | None, datetime | None]:
    """Half-open UTC bounds of the inclusive local dates ``first..last`` (either may be open)."""
    start = None if first is None else utc_bounds(zone, first, first)[0]
    end = None if last is None else utc_bounds(zone, last, last)[1]
    return start, end
