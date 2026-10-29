"""Model-only forecasts: who wins, goals, both teams to score, likely scores (no odds needed).

Football-Data.org sends no prices on the free plan, so the valuation chain (which starts from
a bookmaker's quote) has nothing to value. The Dixon-Coles model does not need prices: it is
fitted on the stored results of each league, so every tracked match gets probabilities and
their fair odds (``1 / p``). There is no expected value and no stake here: those need a price.

A provider match becomes a forecast in four steps:

1. map it to a domain fixture of a tracked league (``football_data_org.map_fixture``);
2. rewrite its teams to their history identity (the model knows teams by that identity);
3. take the league's fitted model as of the kick-off day (cached per league and day);
4. predict a fixed set of markets and the most likely exact scores.

Every match that cannot be forecast says why (:class:`ForecastSkip`) instead of vanishing.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.db.repo import league_teams
from app.domain import DomainValidationError, ExternalRef, Fixture, Market, Selection, Team
from app.logging import get_logger
from app.models import (
    ConfidenceInputs,
    ConfidenceLevel,
    ModelInputError,
    UnknownTeamError,
    assess_confidence,
    predict_markets,
    team_key,
    top_scores,
)
from app.pipeline.context import PipelineContext
from app.pipeline.leagues import HISTORY_SOURCE
from app.pipeline.matches import Match
from app.sports.football_data_org import map_fixture

logger = get_logger(__name__)

FORECAST_MARKETS: Final = (
    Market.match_result(),
    Market.double_chance(),
    Market.totals(1.5),
    Market.totals(2.5),
    Market.totals(3.5),
    Market.btts(),
)
"""What a forecast covers: result, double chance, goal lines and both teams to score."""
TOP_SCORE_COUNT: Final = 3
SECONDS_PER_DAY: Final = 86_400.0


class ForecastSkip(StrEnum):
    """Why a match has no forecast (stable codes; the bot words them)."""

    UNTRACKED_LEAGUE = "untracked_league"
    TEAMS_UNDECIDED = "teams_undecided"
    NO_MODEL = "no_model"
    THIN_HISTORY = "thin_history"
    UNKNOWN_TEAM = "unknown_team"


@dataclass(frozen=True, slots=True)
class Chance:
    """The model probability of one selection."""

    market: Market
    selection: Selection
    probability: float


@dataclass(frozen=True, slots=True)
class LikelyScore:
    """One exact score and its probability."""

    home_goals: int
    away_goals: int
    probability: float


@dataclass(frozen=True, slots=True, kw_only=True)
class Forecast:
    """The model's view of one match, or the reason there is none."""

    match_id: int
    skip: ForecastSkip | None = None
    chances: tuple[Chance, ...] = ()
    scores: tuple[LikelyScore, ...] = ()
    team_matches: int = 0
    confidence: ConfidenceLevel | None = None


class Forecaster:
    """Forecasts provider matches from the stored history (reads the database only)."""

    def __init__(self, ctx: PipelineContext) -> None:
        """Use ``ctx``'s database, league directory and model cache."""
        self._ctx = ctx

    async def forecast(self, matches: Sequence[Match]) -> dict[int, Forecast]:
        """A forecast (or the reason for none) for every match, keyed by provider match id."""
        forecasts: dict[int, Forecast] = {}
        fixtures: dict[int, Fixture] = {}
        for match in matches:
            fixture_or_skip = self._fixture(match)
            if isinstance(fixture_or_skip, ForecastSkip):
                forecasts[match.id] = Forecast(match_id=match.id, skip=fixture_or_skip)
            else:
                fixtures[match.id] = fixture_or_skip
        canonical = await self._canonical(list(fixtures.values()))
        for match_id, fixture in zip(fixtures, canonical, strict=True):
            forecasts[match_id] = await self._forecast(match_id, fixture)
        return forecasts

    def _fixture(self, match: Match) -> Fixture | ForecastSkip:
        code = None if match.competition is None else match.competition.code
        league = self._ctx.leagues.by_provider_code(code)
        if league is None:
            return ForecastSkip.UNTRACKED_LEAGUE
        try:
            fixture = map_fixture(match, league)
        except DomainValidationError as exc:
            logger.warning("forecast_fixture_invalid", match_id=match.id, reason=str(exc))
            return ForecastSkip.TEAMS_UNDECIDED
        return ForecastSkip.TEAMS_UNDECIDED if fixture is None else fixture

    async def _canonical(self, fixtures: list[Fixture]) -> list[Fixture]:
        """``fixtures`` with teams and leagues in their history identity (order kept)."""
        if not fixtures:
            return []
        leagues = {self._ctx.leagues.canonical(f.league.ref) for f in fixtures}
        history: dict[ExternalRef, Sequence[Team]] = {}
        async with self._ctx.sessions() as session:
            for league in leagues:
                history[league] = await league_teams(session, [league], source=HISTORY_SOURCE)
        canonical, _summaries = self._ctx.leagues.canonicalize(fixtures, history)
        return canonical

    async def _forecast(self, match_id: int, fixture: Fixture) -> Forecast:
        league = self._ctx.leagues.canonical(fixture.league.ref)
        fitted = await self._ctx.models.get(league, fixture.kickoff.date())
        model = fitted.model
        if model is None:
            return Forecast(match_id=match_id, skip=ForecastSkip.NO_MODEL)
        home, away = team_key(fixture.home), team_key(fixture.away)
        played = min(fitted.team_matches.get(home, 0), fitted.team_matches.get(away, 0))
        if played < self._ctx.config.min_team_matches:
            return Forecast(match_id=match_id, skip=ForecastSkip.THIN_HISTORY, team_matches=played)
        try:
            predicted = predict_markets(model, home, away, FORECAST_MARKETS)
            matrix = model.score_matrix(home, away)
        except UnknownTeamError:
            return Forecast(match_id=match_id, skip=ForecastSkip.UNKNOWN_TEAM, team_matches=played)
        chances = tuple(
            Chance(market, selection, probabilities.win_probability(selection))
            for market, probabilities in predicted.items()
            for selection in market.selections
        )
        return Forecast(
            match_id=match_id,
            chances=chances,
            scores=tuple(
                LikelyScore(s.home_goals, s.away_goals, s.probability)
                for s in top_scores(matrix, TOP_SCORE_COUNT)
            ),
            team_matches=played,
            confidence=self._confidence(fixture, chances, played),
        )

    def _confidence(
        self, fixture: Fixture, chances: Sequence[Chance], played: int
    ) -> ConfidenceLevel | None:
        """Trust in the most likely result, without a market to compare it with."""
        result = Market.match_result()
        best = max((c.probability for c in chances if c.market == result), default=None)
        if best is None:
            return None
        days = (fixture.kickoff - self._ctx.clock()).total_seconds() / SECONDS_PER_DAY
        try:
            return assess_confidence(
                ConfidenceInputs(
                    min_team_matches=played,
                    xg_available=False,
                    lineups_known=False,
                    model_probability=min(max(best, 0.0), 1.0),
                    market_probability=None,
                    days_to_kickoff=days,
                )
            ).level
        except ModelInputError:
            return None


def chances_of(forecast: Forecast, market: Market) -> Mapping[Selection, float]:
    """The probabilities of ``market``'s selections in ``forecast`` (empty when absent)."""
    return {c.selection: c.probability for c in forecast.chances if c.market == market}
