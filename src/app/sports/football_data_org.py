"""Football :class:`~app.sports.base.SportAdapter` backed by Football-Data.org v4 (ADR 0025).

Fetches and maps only (no storage, no prediction). Request plan (10 requests a minute on the
free plan, no daily limit):

- **Fixtures:** ``GET /matches?dateFrom&dateTo`` (one request per 10 days covers every
  competition of the plan); matches outside the tracked leagues are dropped. Teams:
  ``GET /teams/{id}/matches`` per team for teams known under this provider's identity.
- **Odds:** ``GET /matches?ids=`` in batches; only the averaged 1X2 prices the provider has
  with its odds package, stored as the ``market_avg`` pseudo-bookmaker.
- **Results:** ``GET /matches?ids=`` in batches; the 90-minute score (extra time removed).
- **History:** ``GET /competitions/{code}/matches?season=`` per league (one request each).

Every malformed or undecided record (a cup tie whose teams are not drawn yet) is logged and
skipped. A league outside the key's plan answers 403: it is logged and skipped, the rest of
the call goes on.
"""

from collections.abc import Callable, Collection, Iterable, Sequence
from datetime import UTC, datetime
from itertools import batched
from typing import Final

from app.clients.football_data_org import FootballDataClient
from app.clients.football_data_org_models import Match, MatchStatus, MatchTeam
from app.clients.football_data_org_requests import (
    CompetitionMatchFilters,
    MatchFilters,
    TeamMatchFilters,
)
from app.clients.http import ProviderHttpError
from app.clients.quota import RequestPurpose
from app.domain import (
    BOOKMAKER_MARKET_AVERAGE,
    MIN_DECIMAL_ODDS_EXCLUSIVE,
    DomainValidationError,
    ExternalRef,
    Fixture,
    FixtureResult,
    FixtureStatus,
    HistoryBatch,
    League,
    Market,
    MarketType,
    OddsKind,
    OddsSnapshot,
    ResultStatus,
    Selection,
    Sport,
    Team,
)
from app.logging import get_logger
from app.sports.base import FixtureQuery
from app.sports.football_leagues import (
    FOOTBALL_DATA_ORG_SOURCE,
    LeagueCatalog,
    TrackedLeague,
    season_for,
)

logger = get_logger(__name__)

SOURCE: Final = FOOTBALL_DATA_ORG_SOURCE
SUPPORTED_MARKETS: Final = frozenset({MarketType.MATCH_RESULT})
"""The only market the provider prices (averaged 1X2, with its odds package)."""
MAX_IDS_PER_REQUEST: Final = 20
_FORBIDDEN: Final = 403

type UtcClock = Callable[[], datetime]

_FIXTURE_STATUS: Final = {
    MatchStatus.SCHEDULED: FixtureStatus.SCHEDULED,
    MatchStatus.TIMED: FixtureStatus.SCHEDULED,
    MatchStatus.IN_PLAY: FixtureStatus.LIVE,
    MatchStatus.PAUSED: FixtureStatus.LIVE,
    MatchStatus.EXTRA_TIME: FixtureStatus.LIVE,
    MatchStatus.PENALTY_SHOOTOUT: FixtureStatus.LIVE,
    MatchStatus.FINISHED: FixtureStatus.FINISHED,
    MatchStatus.AWARDED: FixtureStatus.FINISHED,
    MatchStatus.POSTPONED: FixtureStatus.POSTPONED,
    MatchStatus.SUSPENDED: FixtureStatus.ABANDONED,
    MatchStatus.CANCELLED: FixtureStatus.CANCELLED,
}
_RESULT_STATUS: Final = {
    FixtureStatus.FINISHED: ResultStatus.FINISHED,
    FixtureStatus.POSTPONED: ResultStatus.POSTPONED,
    FixtureStatus.CANCELLED: ResultStatus.CANCELLED,
    FixtureStatus.ABANDONED: ResultStatus.ABANDONED,
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def ref(provider_id: int | str) -> ExternalRef:
    """A Football-Data.org identity."""
    return ExternalRef(SOURCE, str(provider_id))


def fixture_status(status: MatchStatus) -> FixtureStatus:
    """The domain status of a provider status (an unknown one reads as scheduled)."""
    return _FIXTURE_STATUS.get(status, FixtureStatus.SCHEDULED)


def _team(team: MatchTeam, country: str | None) -> Team | None:
    if team.id is None or not team.name:
        return None
    return Team(ref=ref(team.id), sport=Sport.FOOTBALL, name=team.name, country=country)


def map_fixture(match: Match, league: TrackedLeague) -> Fixture | None:
    """The domain fixture of ``match`` (``None`` while a team is not decided).

    Raises:
        DomainValidationError: the provider data breaks a domain invariant.
    """
    country = None if match.area is None else match.area.name
    home, away = _team(match.home_team, country), _team(match.away_team, country)
    if home is None or away is None:
        return None
    name = league.provider_code if match.competition is None else match.competition.name
    started = None if match.season is None else match.season.starting_year
    return Fixture(
        ref=ref(match.id),
        sport=Sport.FOOTBALL,
        league=League(ref=league.provider_ref, sport=Sport.FOOTBALL, name=name, country=country),
        season=started if started is not None else season_for(match.utc_date.date()),
        kickoff=match.utc_date,
        home=home,
        away=away,
        status=fixture_status(match.status),
    )


def map_result(match: Match) -> FixtureResult | None:
    """The result of ``match`` once it is over (``None`` while scheduled or live).

    The score is the 90-minute one (markets settle on it); a finished match whose 90-minute
    score the provider does not give is skipped rather than guessed.

    Raises:
        DomainValidationError: the provider data breaks a domain invariant.
    """
    status = _RESULT_STATUS.get(fixture_status(match.status))
    if status is None:
        return None
    if status is not ResultStatus.FINISHED:
        return FixtureResult(fixture=ref(match.id), status=status)
    score = match.score.ninety_minutes
    if score is None or score.home is None or score.away is None:
        logger.warning("result_without_90_minute_score", match_id=match.id)
        return None
    half = match.score.half_time
    both_halves = half.home is not None and half.away is not None
    return FixtureResult(
        fixture=ref(match.id),
        status=status,
        home_score=score.home,
        away_score=score.away,
        home_ht_score=half.home if both_halves else None,
        away_ht_score=half.away if both_halves else None,
    )


def map_odds(match: Match, *, kind: OddsKind, taken_at: datetime) -> list[OddsSnapshot]:
    """The averaged 1X2 prices of ``match`` (empty without the odds package)."""
    home, draw, away = match.odds.home_win, match.odds.draw, match.odds.away_win
    if home is None or draw is None or away is None:
        return []
    prices = ((Selection.HOME, home), (Selection.DRAW, draw), (Selection.AWAY, away))
    if any(price <= MIN_DECIMAL_ODDS_EXCLUSIVE for _, price in prices):
        return []
    return [
        OddsSnapshot(
            fixture=ref(match.id),
            bookmaker=BOOKMAKER_MARKET_AVERAGE,
            market=Market.match_result(),
            selection=selection,
            odds=price,
            taken_at=taken_at,
            kind=kind,
        )
        for selection, price in prices
    ]


def _provider_ids(refs: Iterable[ExternalRef]) -> list[int]:
    ids: list[int] = []
    for item in refs:
        if item.source != SOURCE or not item.external_id.isdecimal():
            logger.warning("fixture_not_from_provider", fixture=str(item))
            continue
        ids.append(int(item.external_id))
    return sorted(set(ids))


class FootballDataAdapter:
    """Football-Data.org implementation of the ``SportAdapter`` protocol."""

    def __init__(
        self,
        client: FootballDataClient,
        *,
        catalog: LeagueCatalog | None = None,
        purpose: RequestPurpose = RequestPurpose.AUTOMATIC,
        clock: UtcClock = _utc_now,
    ) -> None:
        """Adapter over ``client`` for the leagues of ``catalog`` (default: the ten)."""
        self._client = client
        self._catalog = catalog or LeagueCatalog()
        self._purpose = purpose
        self._clock = clock

    def for_purpose(self, purpose: RequestPurpose) -> "FootballDataAdapter":
        """The same adapter on behalf of ``purpose`` (user actions use ``MANUAL``)."""
        return FootballDataAdapter(
            self._client, catalog=self._catalog, purpose=purpose, clock=self._clock
        )

    @property
    def sport(self) -> Sport:
        """Football."""
        return Sport.FOOTBALL

    def supported_markets(self) -> frozenset[MarketType]:
        """1X2 only."""
        return SUPPORTED_MARKETS

    # ------------------------------------------------------------------------ fixtures
    async def fetch_fixtures(self, query: FixtureQuery) -> list[Fixture]:
        """Fixtures in the query's UTC date range and scope (leagues or teams)."""
        wanted = self._wanted_leagues(query)
        matches: dict[int, Match] = {}
        if wanted:
            span = MatchFilters(date_from=query.date_from, date_to=query.date_to)
            for match in await self._client.matches(span, purpose=self._purpose):
                matches.setdefault(match.id, match)
        for match in await self._team_matches(query):
            matches.setdefault(match.id, match)
        fixtures = [
            fixture
            for fixture in self._map_fixtures(matches.values(), wanted, by_team=bool(query.teams))
            if query.date_from <= fixture.kickoff.date() <= query.date_to
        ]
        fixtures.sort(key=lambda fixture: (fixture.kickoff, fixture.ref.external_id))
        return fixtures

    def _wanted_leagues(self, query: FixtureQuery) -> dict[str, TrackedLeague]:
        if not query.leagues:
            leagues = [] if query.teams else list(self._catalog.leagues)
        else:
            leagues = []
            for league_ref in sorted(query.leagues, key=str):
                league = self._catalog.resolve(league_ref)
                if league is None:
                    logger.warning("league_not_tracked", league=str(league_ref))
                    continue
                leagues.append(league)
        return {league.provider_code: league for league in leagues}

    async def _team_matches(self, query: FixtureQuery) -> list[Match]:
        found: list[Match] = []
        for team_ref in sorted(query.teams, key=str):
            if team_ref.source != SOURCE or not team_ref.external_id.isdecimal():
                logger.warning("team_without_provider_id", team=str(team_ref))
                continue
            span = TeamMatchFilters(date_from=query.date_from, date_to=query.date_to)
            found.extend(
                await self._client.team_matches(
                    int(team_ref.external_id), span, purpose=self._purpose
                )
            )
        return found

    def _map_fixtures(
        self, matches: Iterable[Match], wanted: dict[str, TrackedLeague], *, by_team: bool
    ) -> list[Fixture]:
        fixtures: list[Fixture] = []
        for match in matches:
            code = None if match.competition is None else match.competition.code
            league = wanted.get(code or "")
            if league is None and by_team:
                league = self._catalog.by_provider_code(code)
            if league is None:
                continue  # a competition nobody tracks
            try:
                fixture = map_fixture(match, league)
            except DomainValidationError as exc:
                logger.warning("fixture_skipped", match_id=match.id, reason=str(exc))
                continue
            if fixture is None:
                logger.info("fixture_teams_undecided", match_id=match.id)
                continue
            fixtures.append(fixture)
        return fixtures

    # -------------------------------------------------------------------- odds, results
    async def fetch_odds(
        self, fixtures: Sequence[ExternalRef], *, kind: OddsKind
    ) -> list[OddsSnapshot]:
        """Averaged 1X2 prices of ``fixtures`` (none without the provider's odds package)."""
        taken_at = self._clock()
        snapshots: list[OddsSnapshot] = []
        for match in await self._matches_by_id(fixtures, fresh=kind is OddsKind.CLOSING):
            try:
                snapshots.extend(map_odds(match, kind=kind, taken_at=taken_at))
            except DomainValidationError as exc:
                logger.warning("odds_skipped", match_id=match.id, reason=str(exc))
        return snapshots

    async def fetch_results(self, fixtures: Sequence[ExternalRef]) -> list[FixtureResult]:
        """Results of ``fixtures`` that are over; scheduled/live ones are omitted."""
        results: list[FixtureResult] = []
        for match in await self._matches_by_id(fixtures, fresh=True):
            result = self._result_of(match)
            if result is not None:
                results.append(result)
        return results

    def _result_of(self, match: Match) -> FixtureResult | None:
        try:
            return map_result(match)
        except DomainValidationError as exc:
            logger.warning("result_skipped", match_id=match.id, reason=str(exc))
            return None

    async def _matches_by_id(
        self, fixtures: Sequence[ExternalRef], *, fresh: bool = False
    ) -> list[Match]:
        """``fresh``: a result or a closing price must be the latest, never a cached one."""
        found: list[Match] = []
        for chunk in batched(_provider_ids(fixtures), MAX_IDS_PER_REQUEST, strict=False):
            by_id = MatchFilters(ids=tuple(chunk))
            found.extend(await self._client.matches(by_id, purpose=self._purpose, fresh=fresh))
        return found

    async def lineups_confirmed(self, fixture: ExternalRef) -> bool:
        """True when the provider lists both starting elevens of ``fixture``."""
        ids = _provider_ids([fixture])
        if not ids:
            return False
        lineups = await self._client.lineups(ids[0], purpose=self._purpose)
        return lineups.announced

    # ------------------------------------------------------------------------- history
    async def refresh_history(self, leagues: Collection[ExternalRef], season: int) -> HistoryBatch:
        """Every fixture of ``season`` in ``leagues`` with its result (no odds)."""
        fixtures: list[Fixture] = []
        results: list[FixtureResult] = []
        for league_ref in sorted(leagues, key=str):
            league = self._catalog.resolve(league_ref)
            if league is None:
                logger.warning("league_not_tracked", league=str(league_ref))
                continue
            matches = await self._season_matches(league, season)
            mapped = self._map_fixtures(matches, {league.provider_code: league}, by_team=False)
            known = {fixture.ref for fixture in mapped}
            fixtures.extend(mapped)
            results.extend(
                result
                for match in matches
                if (result := self._result_of(match)) is not None and result.fixture in known
            )
        return HistoryBatch(fixtures=tuple(fixtures), results=tuple(results))

    async def _season_matches(self, league: TrackedLeague, season: int) -> list[Match]:
        try:
            return await self._client.competition_matches(
                league.provider_code, CompetitionMatchFilters(season=season), purpose=self._purpose
            )
        except ProviderHttpError as error:
            if error.status_code != _FORBIDDEN:
                raise
            logger.warning("league_outside_plan", league=league.provider_code, season=season)
            return []
