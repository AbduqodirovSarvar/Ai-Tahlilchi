"""In-memory :class:`~app.services.interfaces.DataBrowserService` over the sample world.

Private to the fake backend (the bot's development and tests). It shows the sample leagues,
teams and fixtures as if they were stored, with one morning price per quote, and mimics the
real service's rules: signals are the asking user's only, an administrator refresh has a
minimum gap, and a page past the end is empty.
"""

from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

from app.domain import FixtureStatus, OddsKind, Sport
from app.services._fake_world import FakeFixture
from app.services.errors import CooldownActiveError, NotAuthorizedError, NotFoundError
from app.services.interfaces import (
    MANUAL_REFRESH_MIN_INTERVAL,
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

if TYPE_CHECKING:
    from app.services.fake import FakeState

FAKE_BOOKMAKER: Final = "1xbet"
FAKE_DB_BYTES_PER_ROW: Final = 120
FAKE_REFRESH_FIXTURES: Final = 12
FAKE_REFRESH_ODDS: Final = 150
FAKE_REFRESH_REQUESTS: Final = 7
MORNING_LEAD: Final = timedelta(hours=20)
"""The fake's morning price is taken this long before kick-off."""


def _status(fixture: FakeFixture, now: datetime) -> FixtureStatus:
    if fixture.finished(now):
        return FixtureStatus.FINISHED
    return FixtureStatus.LIVE if fixture.started(now) else FixtureStatus.SCHEDULED


def _stored(fixture: FakeFixture, now: datetime) -> StoredFixture:
    finished = fixture.finished(now) and fixture.result_reported
    return StoredFixture(
        fixture_id=fixture.fixture_id,
        sport=fixture.sport,
        league_id=fixture.league.league_id,
        league=fixture.league.name,
        country=fixture.league.country,
        home_team=fixture.home.name,
        away_team=fixture.away.name,
        home_team_id=fixture.home.team_id,
        away_team_id=fixture.away.team_id,
        kickoff=fixture.kickoff,
        status=_status(fixture, now),
        home_score=fixture.home_score if finished else None,
        away_score=fixture.away_score if finished else None,
        has_odds=True,
        source="fake",
    )


def _odds(fixture: FakeFixture) -> tuple[StoredOdds, ...]:
    taken_at = fixture.kickoff - MORNING_LEAD
    return tuple(
        StoredOdds(
            bookmaker=FAKE_BOOKMAKER,
            market=quote.market.type,
            selection=quote.selection,
            line=quote.market.line or 0.0,
            odds=quote.odds,
            kind=OddsKind.MORNING,
            taken_at=taken_at,
        )
        for quote in fixture.quotes
    )


def _pages(total: int, size: int) -> int:
    return -(-total // size)


def _local_bounds(
    zone: ZoneInfo, first: date | None, last: date | None
) -> tuple[datetime | None, datetime | None]:
    start = None if first is None else datetime.combine(first, time(0), tzinfo=zone)
    end = None if last is None else datetime.combine(last + timedelta(days=1), time(0), tzinfo=zone)
    return (
        None if start is None else start.astimezone(UTC),
        None if end is None else end.astimezone(UTC),
    )


class FakeDataBrowserService:
    """Sample-world implementation of the database browser."""

    def __init__(self, state: "FakeState", *, admin_ids: frozenset[int] | None = None) -> None:
        """Browse ``state``'s world; ``admin_ids=None`` lets everyone refresh."""
        self._state = state
        self._admin_ids = admin_ids
        self._refreshed_at: datetime | None = None
        self._last_manual: datetime | None = None

    def _fixtures(self) -> tuple[FakeFixture, ...]:
        return self._state.world.fixtures

    def _find(self, fixture_id: EntityId) -> FakeFixture:
        for fixture in self._fixtures():
            if fixture.fixture_id == fixture_id:
                return fixture
        raise NotFoundError("fixture", fixture_id)

    async def database_overview(self, user_id: int) -> DatabaseOverview:
        """See :meth:`DataBrowserService.database_overview`."""
        now = self._state.now()
        world = self._state.world
        fixtures = self._fixtures()
        odds_rows = sum(len(fixture.quotes) for fixture in fixtures)
        own = len(self._state.user_signals(user_id))
        tables = (
            StoredTable(name="leagues", rows=len(world.leagues), last_updated_at=now),
            StoredTable(name="teams", rows=len(world.teams), last_updated_at=now),
            StoredTable(name="fixtures", rows=len(fixtures), last_updated_at=now),
            StoredTable(name="odds_snapshots", rows=odds_rows, last_updated_at=now),
            StoredTable(name="signals", rows=own, own=True),
        )
        return DatabaseOverview(
            tables=tables,
            size_bytes=sum(table.rows for table in tables) * FAKE_DB_BYTES_PER_ROW,
            last_refresh_at=self._refreshed_at or now,
            earliest_kickoff=fixtures[0].kickoff if fixtures else None,
            latest_kickoff=fixtures[-1].kickoff if fixtures else None,
            upcoming_fixtures=sum(1 for fixture in fixtures if fixture.kickoff >= now),
        )

    async def list_stored_leagues(
        self, user_id: int, *, sport: Sport | None = None
    ) -> tuple[StoredLeague, ...]:
        """See :meth:`DataBrowserService.list_stored_leagues`."""
        _ = user_id
        now = self._state.now()
        leagues = []
        for league in sorted(self._state.world.leagues, key=lambda item: item.name):
            if sport is not None and league.sport is not sport:
                continue
            own = [f for f in self._fixtures() if f.league.league_id == league.league_id]
            leagues.append(
                StoredLeague(
                    league_id=league.league_id,
                    sport=league.sport,
                    name=league.name,
                    country=league.country,
                    source="fake",
                    fixtures=len(own),
                    upcoming=sum(1 for f in own if f.kickoff >= now),
                    first_kickoff=own[0].kickoff if own else None,
                    last_kickoff=own[-1].kickoff if own else None,
                )
            )
        return tuple(leagues)

    async def list_stored_fixtures(
        self, user_id: int, request: StoredFixtureRequest
    ) -> StoredFixturePage:
        """See :meth:`DataBrowserService.list_stored_fixtures`."""
        now = self._state.now()
        zone = self._state.settings_for(user_id).zone
        start, end = _local_bounds(zone, request.date_from, request.date_to)
        matches = [
            _stored(f, now)
            for f in self._fixtures()
            if (start is None or f.kickoff >= start)
            and (end is None or f.kickoff < end)
            and (not request.league_ids or f.league.league_id in request.league_ids)
            and (not request.team_ids or {f.home.team_id, f.away.team_id} & request.team_ids)
            and (not request.statuses or _status(f, now) in request.statuses)
            and (request.with_odds is None or request.with_odds)
        ]
        first = request.page * request.page_size
        return StoredFixturePage(
            items=tuple(matches[first : first + request.page_size]),
            page=request.page,
            page_count=_pages(len(matches), request.page_size),
            total=len(matches),
        )

    async def get_stored_fixture(self, user_id: int, fixture_id: EntityId) -> StoredFixtureDetail:
        """See :meth:`DataBrowserService.get_stored_fixture`."""
        fixture = self._find(fixture_id)
        now = self._state.now()
        odds = _odds(fixture)
        shown = odds[:MAX_STORED_ODDS_IN_DETAIL]
        return StoredFixtureDetail(
            fixture=_stored(fixture, now),
            season=fixture.kickoff.year,
            home_ht_score=None,
            away_ht_score=None,
            home_xg=None,
            away_xg=None,
            odds=(BookmakerOdds(bookmaker=FAKE_BOOKMAKER, quotes=shown),),
            odds_total=len(odds),
            truncated=len(odds) > len(shown),
            signals=tuple(
                self._state.view(stored)
                for stored in self._state.user_signals(user_id)
                if stored.fixture.fixture_id == fixture_id
            ),
        )

    async def list_stored_teams(self, user_id: int, request: StoredTeamRequest) -> StoredTeamPage:
        """See :meth:`DataBrowserService.list_stored_teams`."""
        _ = user_id
        needle = request.text.strip().casefold()
        matches = []
        for team in sorted(self._state.world.teams, key=lambda item: item.name):
            fixtures = [
                f for f in self._fixtures() if team.team_id in (f.home.team_id, f.away.team_id)
            ]
            in_league = not request.league_ids or any(
                f.league.league_id in request.league_ids for f in fixtures
            )
            if (
                needle in team.name.casefold()
                and (request.sport is None or team.sport is request.sport)
                and in_league
            ):
                matches.append(
                    StoredTeam(
                        team_id=team.team_id,
                        sport=team.sport,
                        name=team.name,
                        country=None,
                        source="fake",
                        fixtures=len(fixtures),
                    )
                )
        first = request.page * request.page_size
        return StoredTeamPage(
            items=tuple(matches[first : first + request.page_size]),
            page=request.page,
            page_count=_pages(len(matches), request.page_size),
            total=len(matches),
        )

    async def list_stored_odds(self, user_id: int, request: StoredOddsRequest) -> StoredOddsPage:
        """See :meth:`DataBrowserService.list_stored_odds`."""
        _ = user_id
        rows = [
            quote
            for quote in _odds(self._find(request.fixture_id))
            if (request.bookmaker is None or quote.bookmaker == request.bookmaker)
            and (request.market is None or quote.market is request.market)
        ]
        first = request.page * request.page_size
        return StoredOddsPage(
            items=tuple(rows[first : first + request.page_size]),
            page=request.page,
            page_count=_pages(len(rows), request.page_size),
            total=len(rows),
        )

    async def refresh_database(self, user_id: int) -> RefreshReport:
        """See :meth:`DataBrowserService.refresh_database`."""
        if self._admin_ids is not None and user_id not in self._admin_ids:
            raise NotAuthorizedError(user_id, "refresh the database")
        now = self._state.now()
        if self._last_manual is not None and now - self._last_manual < MANUAL_REFRESH_MIN_INTERVAL:
            raise CooldownActiveError(MANUAL_REFRESH_MIN_INTERVAL - (now - self._last_manual))
        self._last_manual = self._refreshed_at = now
        return RefreshReport(
            started_at=now,
            finished_at=self._state.now(),
            fixtures=FAKE_REFRESH_FIXTURES,
            results=0,
            odds=FAKE_REFRESH_ODDS,
            api_requests=FAKE_REFRESH_REQUESTS,
        )
