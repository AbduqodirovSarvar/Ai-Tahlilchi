"""Football :class:`~app.sports.base.SportAdapter` backed by API-Football v3.

Fetches and maps only (no storage, no prediction). Request-saving strategy (plan §9):

- **Fixtures:** for the tracked leagues, ``fixtures?date=`` per day (1 request, every
  league) when the window has fewer days than there are league-seasons to ask, otherwise
  ``fixtures?league&season&from&to`` per league-season. Teams: ``fixtures?team&season&
  from&to`` per team and season.
- **Odds:** fixtures seen by :meth:`fetch_fixtures` are grouped by league and day; a group
  of two or more is fetched with ``odds?league&season&date`` (paginated), anything else
  with ``odds?fixture``. Odds for future days are cached 6 hours.
- **Results:** ``fixtures?ids=`` in batches of 20.
- **History:** ``fixtures?league&season`` per league (one request per league-season).

Every malformed record is logged and skipped. The adapter spends quota as
:attr:`~app.clients.quota.RequestPurpose.AUTOMATIC` unless built for manual use
(:meth:`FootballAdapter.for_purpose`).
"""

from collections import OrderedDict, defaultdict
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Final

from app.clients.api_football import ApiFootballClient
from app.clients.api_football_models import FixtureItem
from app.clients.quota import RequestPurpose
from app.domain import (
    DomainValidationError,
    ExternalRef,
    Fixture,
    FixtureResult,
    FixtureStatus,
    HistoryBatch,
    MarketType,
    OddsKind,
    OddsSnapshot,
    Sport,
)
from app.logging import get_logger
from app.sports.base import FixtureQuery
from app.sports.football_leagues import LeagueCatalog, TrackedLeague, seasons_between
from app.sports.football_mapping import (
    SOURCE,
    SUPPORTED_MARKETS,
    HandicapConvention,
    PlayerAbsence,
    TeamLineup,
    expected_goals,
    map_fixture,
    map_injuries,
    map_lineups,
    map_odds,
    map_result,
    ref,
)
from app.sports.team_matching import IdentityMap

logger = get_logger(__name__)

type UtcClock = Callable[[], datetime]

FUTURE_ODDS_TTL_SECONDS: Final = 6 * 3600.0
MIN_FIXTURES_FOR_LEAGUE_ODDS: Final = 2
HOME_AND_AWAY: Final = 2
# Fixture → (league, season, day) remembered for odds grouping; bounded memory.
MAX_REMEMBERED_FIXTURES: Final = 5_000


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class _OddsGroupKey:
    league_id: int
    season: int
    day: date


class FootballAdapter:
    """API-Football implementation of the ``SportAdapter`` protocol (plus context extras)."""

    def __init__(  # noqa: PLR0913 - every collaborator is injectable for tests
        self,
        client: ApiFootballClient,
        *,
        catalog: LeagueCatalog | None = None,
        identity: IdentityMap | None = None,
        purpose: RequestPurpose = RequestPurpose.AUTOMATIC,
        clock: UtcClock = _utc_now,
        handicap_convention: HandicapConvention = HandicapConvention.HOME_LINE,
    ) -> None:
        """Adapter over ``client`` for the leagues of ``catalog`` (default: the ten)."""
        self._client = client
        self._catalog = catalog or LeagueCatalog()
        self._identity = identity or IdentityMap()
        self._purpose = purpose
        self._clock = clock
        self._convention = handicap_convention
        self._seen: OrderedDict[int, _OddsGroupKey] = OrderedDict()

    def for_purpose(self, purpose: RequestPurpose) -> "FootballAdapter":
        """The same adapter charging ``purpose`` (user actions use ``MANUAL``)."""
        adapter = FootballAdapter(
            self._client,
            catalog=self._catalog,
            identity=self._identity,
            purpose=purpose,
            clock=self._clock,
            handicap_convention=self._convention,
        )
        adapter._seen = self._seen  # share odds grouping knowledge
        return adapter

    def with_identity(self, identity: IdentityMap) -> "FootballAdapter":
        """The same adapter emitting canonical (history) teams and leagues."""
        adapter = self.for_purpose(self._purpose)
        adapter._identity = identity
        return adapter

    @property
    def sport(self) -> Sport:
        """Football."""
        return Sport.FOOTBALL

    def supported_markets(self) -> frozenset[MarketType]:
        """1X2, double chance, totals, BTTS and Asian handicap."""
        return SUPPORTED_MARKETS

    # ------------------------------------------------------------------ fixtures

    async def fetch_fixtures(self, query: FixtureQuery) -> list[Fixture]:
        """Fixtures in the query's UTC date range and scope (leagues or teams)."""
        leagues = self._leagues_of(query)
        items: dict[int, FixtureItem] = {}
        if leagues:
            for item in await self._league_items(leagues, query.date_from, query.date_to):
                items.setdefault(item.fixture.id, item)
        for item in await self._team_items(query.teams, query.date_from, query.date_to):
            items.setdefault(item.fixture.id, item)
        fixtures = [
            fixture
            for fixture in self._map_fixtures(items.values())
            if query.date_from <= fixture.kickoff.date() <= query.date_to
        ]
        fixtures.sort(key=lambda fixture: (fixture.kickoff, fixture.ref.external_id))
        return fixtures

    def _leagues_of(self, query: FixtureQuery) -> list[TrackedLeague]:
        if not query.leagues:
            return [] if query.teams else list(self._catalog.leagues)
        leagues: list[TrackedLeague] = []
        for league_ref in sorted(query.leagues, key=str):
            league = self._catalog.resolve(league_ref)
            if league is None:
                logger.warning("league_not_tracked", league=str(league_ref))
                continue
            leagues.append(league)
        return leagues

    async def _league_items(
        self, leagues: Sequence[TrackedLeague], date_from: date, date_to: date
    ) -> list[FixtureItem]:
        seasons = seasons_between(date_from, date_to)
        days = (date_to - date_from).days + 1
        if days < len(leagues) * len(seasons):
            wanted = {league.api_id for league in leagues}
            by_day: list[FixtureItem] = []
            for offset in range(days):
                day = date_from + timedelta(days=offset)
                items = await self._client.fixtures_by_date(day, purpose=self._purpose)
                by_day.extend(item for item in items if item.league.id in wanted)
            return by_day
        by_league: list[FixtureItem] = []
        for league in leagues:
            for season in seasons:
                by_league.extend(
                    await self._client.fixtures_by_league(
                        league.api_id,
                        season,
                        purpose=self._purpose,
                        date_from=date_from,
                        date_to=date_to,
                    )
                )
        return by_league

    async def _team_items(
        self, teams: Collection[ExternalRef], date_from: date, date_to: date
    ) -> list[FixtureItem]:
        items: list[FixtureItem] = []
        for team_ref in sorted(teams, key=str):
            team_id = self._provider_team_id(team_ref)
            if team_id is None:
                logger.warning("team_without_provider_id", team=str(team_ref))
                continue
            for season in seasons_between(date_from, date_to):
                items.extend(
                    await self._client.fixtures_by_team(
                        team_id,
                        season,
                        date_from=date_from,
                        date_to=date_to,
                        purpose=self._purpose,
                    )
                )
        return items

    def _provider_team_id(self, team_ref: ExternalRef) -> int | None:
        provider = team_ref if team_ref.source == SOURCE else self._identity.provider_ref(team_ref)
        return None if provider is None else _numeric_id(provider)

    def _map_fixtures(self, items: Iterable[FixtureItem]) -> list[Fixture]:
        fixtures: list[Fixture] = []
        for item in items:
            try:
                fixture = map_fixture(item)
            except DomainValidationError as exc:
                logger.warning("fixture_skipped", fixture_id=item.fixture.id, reason=str(exc))
                continue
            self._remember(item)
            fixtures.append(self._identity.apply(fixture))
        return fixtures

    def _remember(self, item: FixtureItem) -> None:
        kickoff_day = item.fixture.date.astimezone(UTC).date()
        self._seen[item.fixture.id] = _OddsGroupKey(item.league.id, item.league.season, kickoff_day)
        self._seen.move_to_end(item.fixture.id)
        while len(self._seen) > MAX_REMEMBERED_FIXTURES:
            self._seen.popitem(last=False)

    # ------------------------------------------------------------------ odds

    async def fetch_odds(
        self, fixtures: Sequence[ExternalRef], *, kind: OddsKind
    ) -> list[OddsSnapshot]:
        """Current prices of ``fixtures`` in the supported markets, tagged with ``kind``."""
        wanted = _provider_ids(fixtures)
        groups: defaultdict[_OddsGroupKey, list[int]] = defaultdict(list)
        singles: list[int] = []
        for fixture_id in wanted:
            key = self._seen.get(fixture_id)
            if key is None:
                singles.append(fixture_id)
            else:
                groups[key].append(fixture_id)
        items = []
        today = self._clock().astimezone(UTC).date()
        for key, members in groups.items():
            ttl = FUTURE_ODDS_TTL_SECONDS if key.day > today else None
            if len(members) < MIN_FIXTURES_FOR_LEAGUE_ODDS:
                singles.extend(members)
                continue
            items.extend(
                await self._client.odds_by_league_date(
                    key.league_id, key.season, key.day, purpose=self._purpose, cache_ttl=ttl
                )
            )
        for fixture_id in singles:
            key = self._seen.get(fixture_id)
            ttl = FUTURE_ODDS_TTL_SECONDS if key is not None and key.day > today else None
            items.extend(
                await self._client.odds_by_fixture(fixture_id, purpose=self._purpose, cache_ttl=ttl)
            )
        relevant = [item for item in items if item.fixture.id in wanted]
        snapshots, stats = map_odds(
            relevant, kind=kind, taken_at=self._clock(), convention=self._convention
        )
        if stats.invalid_prices:
            logger.warning("odds_prices_skipped", count=stats.invalid_prices)
        return snapshots

    # ------------------------------------------------------------------ results

    async def fetch_results(self, fixtures: Sequence[ExternalRef]) -> list[FixtureResult]:
        """Results of ``fixtures`` that are over; scheduled/live ones are omitted."""
        items = await self._client.fixtures_by_ids(
            sorted(_provider_ids(fixtures)), purpose=self._purpose
        )
        return [result for item in items if (result := self._result_of(item)) is not None]

    def _result_of(self, item: FixtureItem) -> FixtureResult | None:
        try:
            return map_result(item)
        except DomainValidationError as exc:
            logger.warning("result_skipped", fixture_id=item.fixture.id, reason=str(exc))
            return None

    async def with_expected_goals(self, results: Sequence[FixtureResult]) -> list[FixtureResult]:
        """``results`` with xG filled in for finished fixtures (1 request each).

        Plan §4 step 0: call it only for *newly* finished fixtures. A failure for one
        fixture keeps that result without xG.
        """
        enriched: list[FixtureResult] = []
        for result in results:
            fixture_id = _numeric_id(result.fixture)
            if result.status.value != FixtureStatus.FINISHED.value or fixture_id is None:
                enriched.append(result)
                continue
            enriched.append(await self._add_xg(result, fixture_id))
        return enriched

    async def _add_xg(self, result: FixtureResult, fixture_id: int) -> FixtureResult:
        items = await self._client.fixture_statistics(fixture_id, purpose=self._purpose)
        # The provider lists the home team first, then the away team.
        values = [expected_goals([item]).get(ref(item.team.id)) for item in items]
        if len(values) != HOME_AND_AWAY:
            return result
        home_xg, away_xg = values
        if home_xg is None or away_xg is None:
            return result
        return replace(result, home_xg=home_xg, away_xg=away_xg)

    # ------------------------------------------------------------------ history

    async def refresh_history(self, leagues: Collection[ExternalRef], season: int) -> HistoryBatch:
        """Every fixture of ``season`` in ``leagues`` with its result (no odds)."""
        fixtures: list[Fixture] = []
        results: list[FixtureResult] = []
        for league_ref in sorted(leagues, key=str):
            league = self._catalog.resolve(league_ref)
            if league is None:
                logger.warning("league_not_tracked", league=str(league_ref))
                continue
            items = await self._client.fixtures_by_league(
                league.api_id, season, purpose=self._purpose
            )
            mapped = {fixture.ref: fixture for fixture in self._map_fixtures(items)}
            fixtures.extend(mapped.values())
            results.extend(
                result
                for item in items
                if (result := self._result_of_mapped(item, mapped)) is not None
            )
        return HistoryBatch(fixtures=tuple(fixtures), results=tuple(results))

    def _result_of_mapped(
        self, item: FixtureItem, mapped: dict[ExternalRef, Fixture]
    ) -> FixtureResult | None:
        result = self._result_of(item)
        return result if result is not None and result.fixture in mapped else None

    # ------------------------------------------------------------------ context

    async def fetch_injuries(self, fixture: ExternalRef) -> list[PlayerAbsence]:
        """Missing and doubtful players of one fixture (only worth it 0-3 days ahead)."""
        fixture_id = _numeric_id(fixture)
        if fixture_id is None:
            return []
        items = await self._client.injuries_by_fixture(fixture_id, purpose=self._purpose)
        return map_injuries(items)

    async def fetch_lineups(self, fixture: ExternalRef) -> list[TeamLineup]:
        """Confirmed line-ups (empty list until announced, ~40 minutes before kick-off)."""
        fixture_id = _numeric_id(fixture)
        if fixture_id is None:
            return []
        items = await self._client.lineups(fixture_id, purpose=self._purpose)
        return map_lineups(items)


def _numeric_id(provider_ref: ExternalRef) -> int | None:
    if provider_ref.source != SOURCE or not provider_ref.external_id.isdigit():
        return None
    return int(provider_ref.external_id)


def _provider_ids(refs: Iterable[ExternalRef]) -> set[int]:
    ids: set[int] = set()
    for fixture_ref in refs:
        fixture_id = _numeric_id(fixture_ref)
        if fixture_id is None:
            logger.warning("fixture_not_from_provider", fixture=str(fixture_ref))
            continue
        ids.add(fixture_id)
    return ids
