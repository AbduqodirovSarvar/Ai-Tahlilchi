"""Synthetic league history with a perfectly efficient betting market.

Purpose: validate the backtest end to end. Scores are drawn from a Dixon-Coles process whose
team strengths drift between seasons. For every match the *market* knows the true expected
goals, including a per-match shock (injuries, motivation, weather) that the model can never
learn from past scores; prices are the true probabilities with the margin loaded Shin-style
(more on longshots, like real books) and rounded to 2 decimals like real odds. Outcomes are
drawn from those same probabilities.

On such a market no strategy can win in expectation: every bet's expected return is
negative (about ``-margin``, less where pushes are possible, more on longshots). A backtest
that shows a significant edge here is leaking information or is optimistic, which a test
asserts.
"""

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final

import numpy as np
import numpy.typing as npt
from scipy.optimize import brentq

from app.domain import (
    BOOKMAKER_MARKET_AVERAGE,
    BOOKMAKER_PINNACLE,
    ExternalRef,
    Fixture,
    FixtureResult,
    FixtureStatus,
    HistoryBatch,
    League,
    Market,
    OddsKind,
    OddsSnapshot,
    ResultStatus,
    Sport,
    Team,
)
from app.models.ev import asian_fair_odds
from app.models.exceptions import ModelInputError
from app.models.outcomes import market_from_matrix
from app.models.poisson import MAX_EXPECTED_GOALS, feasible_rho, score_matrix

type FloatArray = npt.NDArray[np.float64]

SOURCE: Final = "synthetic"
TOTALS_LINE: Final = 2.5
MAX_HANDICAP_LINE: Final = 3.0
_KICKOFF: Final = time(15, 0, tzinfo=UTC)
_SEASON_START_MONTH: Final = 8
_SATURDAY: Final = 5
_OPENING_LEAD: Final = timedelta(days=1)
_ODDS_DECIMALS: Final = 2
_QUARTERS_PER_UNIT: Final = 4
_MIN_TEAMS: Final = 4
_SHIN_XTOL: Final = 1e-12


@dataclass(frozen=True, slots=True, kw_only=True)
class BookmakerProfile:
    """A bookmaker's margin at one moment."""

    bookmaker: str
    kind: OddsKind
    margin: float


DEFAULT_BOOKMAKERS: Final = (
    BookmakerProfile(bookmaker=BOOKMAKER_PINNACLE, kind=OddsKind.OPENING, margin=0.03),
    BookmakerProfile(bookmaker=BOOKMAKER_PINNACLE, kind=OddsKind.CLOSING, margin=0.025),
    BookmakerProfile(bookmaker=BOOKMAKER_MARKET_AVERAGE, kind=OddsKind.OPENING, margin=0.055),
    BookmakerProfile(bookmaker=BOOKMAKER_MARKET_AVERAGE, kind=OddsKind.CLOSING, margin=0.05),
)


@dataclass(frozen=True, slots=True, kw_only=True)
class SyntheticConfig:
    """Size and parameters of the synthetic history (all reported for reproducibility)."""

    leagues: int = 3
    teams_per_league: int = 20
    seasons: int = 5
    first_season: int = 2019
    seed: int = 20261021
    intercept: float = 0.15
    home_advantage: float = 0.25
    rho: float = -0.08
    strength_sd: float = 0.3
    season_drift_sd: float = 0.1
    match_shock_sd: float = 0.15
    bookmakers: tuple[BookmakerProfile, ...] = DEFAULT_BOOKMAKERS

    def __post_init__(self) -> None:
        """An even number of teams (round robin) and at least one league and season."""
        if self.teams_per_league < _MIN_TEAMS or self.teams_per_league % 2:
            msg = f"teams_per_league must be even and >= {_MIN_TEAMS}: {self.teams_per_league}"
            raise ModelInputError(msg)
        if self.leagues < 1 or self.seasons < 1:
            msg = f"need at least one league and one season: {self}"
            raise ModelInputError(msg)


def round_robin(n_teams: int) -> list[list[tuple[int, int]]]:
    """Double round robin (circle method): each round every team plays exactly once."""
    order = list(range(n_teams))
    first_half = []
    for round_number in range(n_teams - 1):
        pairs = []
        for slot in range(n_teams // 2):
            home, away = order[slot], order[n_teams - 1 - slot]
            pairs.append((home, away) if (round_number + slot) % 2 == 0 else (away, home))
        first_half.append(pairs)
        order = [order[0], order[-1], *order[1:-1]]
    return first_half + [[(away, home) for home, away in pairs] for pairs in first_half]


def generate_history(config: SyntheticConfig) -> HistoryBatch:
    """Fixtures, finished results and opening/closing odds of every synthetic league."""
    rng = np.random.default_rng(config.seed)
    fixtures: list[Fixture] = []
    results: list[FixtureResult] = []
    odds: list[OddsSnapshot] = []
    for league_number in range(config.leagues):
        league = League(
            ref=ExternalRef(SOURCE, f"L{league_number}"),
            sport=Sport.FOOTBALL,
            name=f"Synthetic League {league_number}",
        )
        teams = [
            Team(
                ref=ExternalRef(SOURCE, f"L{league_number}-T{n:02d}"),
                sport=Sport.FOOTBALL,
                name=f"L{league_number} Team {n:02d}",
            )
            for n in range(config.teams_per_league)
        ]
        attack = _centred(rng.normal(0.0, config.strength_sd, len(teams)))
        defence = _centred(rng.normal(0.0, config.strength_sd, len(teams)))
        for season in range(config.first_season, config.first_season + config.seasons):
            context = _SeasonContext(config, league, teams, season, attack, defence)
            _simulate_season(rng, context, fixtures, results, odds)
            attack = _centred(attack + rng.normal(0.0, config.season_drift_sd, len(teams)))
            defence = _centred(defence + rng.normal(0.0, config.season_drift_sd, len(teams)))
    return HistoryBatch(fixtures=tuple(fixtures), results=tuple(results), odds=tuple(odds))


@dataclass(frozen=True, slots=True)
class _SeasonContext:
    config: SyntheticConfig
    league: League
    teams: list[Team]
    season: int
    attack: FloatArray
    defence: FloatArray


def _simulate_season(
    rng: np.random.Generator,
    context: _SeasonContext,
    fixtures: list[Fixture],
    results: list[FixtureResult],
    odds: list[OddsSnapshot],
) -> None:
    first_round = _first_saturday(date(context.season, _SEASON_START_MONTH, 1))
    for round_number, pairs in enumerate(round_robin(len(context.teams))):
        kickoff = datetime.combine(first_round + timedelta(weeks=round_number), _KICKOFF)
        for home, away in pairs:
            fixture = Fixture(
                ref=ExternalRef(
                    SOURCE,
                    f"{context.league.ref.external_id}/{context.season}/{round_number:02d}/{home:02d}-{away:02d}",
                ),
                sport=Sport.FOOTBALL,
                league=context.league,
                season=context.season,
                kickoff=kickoff,
                home=context.teams[home],
                away=context.teams[away],
                status=FixtureStatus.FINISHED,
            )
            matrix = _true_matrix(rng, context, home, away)
            cell = int(rng.choice(matrix.size, p=matrix.ravel()))
            home_goals, away_goals = divmod(cell, matrix.shape[1])
            fixtures.append(fixture)
            results.append(
                FixtureResult(
                    fixture=fixture.ref,
                    status=ResultStatus.FINISHED,
                    home_score=home_goals,
                    away_score=away_goals,
                )
            )
            odds.extend(_prices(fixture, matrix, context.config.bookmakers))


def _true_matrix(
    rng: np.random.Generator, context: _SeasonContext, home: int, away: int
) -> FloatArray:
    """Score distribution the market knows: team strengths plus this match's shocks."""
    config = context.config
    shock_home, shock_away = rng.normal(0.0, config.match_shock_sd, 2)
    log_home = (
        config.intercept
        + config.home_advantage
        + context.attack[home]
        - context.defence[away]
        + shock_home
    )
    log_away = config.intercept + context.attack[away] - context.defence[home] + shock_away
    home_rate = min(math.exp(log_home), MAX_EXPECTED_GOALS)
    away_rate = min(math.exp(log_away), MAX_EXPECTED_GOALS)
    rho = feasible_rho(home_rate, away_rate, config.rho)
    return score_matrix(home_rate, away_rate, rho)


def _prices(
    fixture: Fixture, matrix: FloatArray, bookmakers: tuple[BookmakerProfile, ...]
) -> list[OddsSnapshot]:
    snapshots: list[OddsSnapshot] = []
    for market in _markets(matrix):
        probabilities = market_from_matrix(matrix, market)
        fair = {
            selection: 1.0 / asian_fair_odds(outcome)
            for selection, outcome in probabilities.outcomes.items()
        }
        for profile in bookmakers:
            booked = dict(zip(fair, shin_prices(tuple(fair.values()), profile.margin), strict=True))
            if min(booked.values()) <= 1.0:
                continue  # too short to quote at this margin; the whole market is skipped
            taken_at = (
                fixture.kickoff - _OPENING_LEAD
                if profile.kind is OddsKind.OPENING
                else fixture.kickoff
            )
            snapshots.extend(
                OddsSnapshot(
                    fixture=fixture.ref,
                    bookmaker=profile.bookmaker,
                    market=market,
                    selection=selection,
                    odds=price,
                    taken_at=taken_at,
                    kind=profile.kind,
                )
                for selection, price in booked.items()
            )
    return snapshots


def shin_prices(probabilities: tuple[float, ...], margin: float) -> tuple[float, ...]:
    """Odds with ``margin`` loaded the way Shin's model does (more on longshots).

    Forward Shin model: ``pi_i = beta * sqrt(z p_i + (1 - z) p_i^2)`` with ``beta`` the
    sum of the roots, so the booksum is ``beta^2``; the insider share ``z`` is solved so that
    the booksum is ``1 + margin``. Shin de-vig therefore recovers ``probabilities`` exactly
    (before the 2-decimal rounding real odds have).
    """
    truth = np.asarray(probabilities, dtype=np.float64)

    def roots(insider_share: float) -> FloatArray:
        return np.asarray(
            np.sqrt(insider_share * truth + (1.0 - insider_share) * truth * truth),
            dtype=np.float64,
        )

    def excess(insider_share: float) -> float:
        return float(roots(insider_share).sum()) ** 2 - (1.0 + margin)

    if margin <= 0.0 or excess(1.0) <= 0.0:
        msg = f"margin {margin!r} cannot be reached for probabilities {probabilities}"
        raise ModelInputError(msg)
    insider_share = float(brentq(excess, 0.0, 1.0, xtol=_SHIN_XTOL))
    weights = roots(insider_share)
    implied = weights * weights.sum()
    return tuple(round(float(1.0 / value), _ODDS_DECIMALS) for value in implied)


def _markets(matrix: FloatArray) -> tuple[Market, ...]:
    """1X2, total 2.5 and the Asian handicap closest to the expected goal difference."""
    goals = np.arange(matrix.shape[0], dtype=np.float64)
    supremacy = float(goals @ matrix.sum(axis=1) - goals @ matrix.sum(axis=0))
    quarters = round(-supremacy * _QUARTERS_PER_UNIT) / _QUARTERS_PER_UNIT
    line = min(max(quarters, -MAX_HANDICAP_LINE), MAX_HANDICAP_LINE)
    return (Market.match_result(), Market.totals(TOTALS_LINE), Market.asian_handicap(line))


def _first_saturday(day: date) -> date:
    return day + timedelta(days=(_SATURDAY - day.weekday()) % 7)


def _centred(values: FloatArray) -> FloatArray:
    return np.asarray(values - values.mean(), dtype=np.float64)
