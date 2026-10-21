"""Probabilities keyed by the domain's ``Market`` / ``Selection``, and bet settlement.

Every selection is described by the probabilities of its five settlement outcomes
(:class:`AsianOutcomeProbabilities`); a plain win/lose bet is the binary special case. One
representation for every market keeps EV, Kelly, ensembling and settlement in one code path
(quarter-line Asian handicaps included).

Double chance is the exception to "selections partition the outcome space": its three
selections overlap (each match outcome belongs to two of them), so only per-selection
distributions are meaningful there, not a sum over selections.
"""

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from app.domain import Market, MarketType, Selection
from app.models.asian import AsianOutcomeProbabilities, Settlement, settle
from app.models.exceptions import ModelInputError
from app.models.poisson import DixonColesModel, FloatArray
from app.models.score_markets import (
    asian_handicap,
    both_teams_to_score,
    double_chance,
    match_result,
    total_goals,
)

# Win probabilities of a partition market must sum to 1 within this tolerance.
PARTITION_SUM_TOLERANCE: Final = 1e-9
_HALF_LINE_QUARTERS: Final = 2
_QUARTERS_PER_UNIT: Final = 4


@dataclass(frozen=True, slots=True)
class MarketProbabilities:
    """Settlement distributions of every selection of one market."""

    market: Market
    outcomes: Mapping[Selection, AsianOutcomeProbabilities]

    def __post_init__(self) -> None:
        """Require exactly the market's selections; partition markets must sum to 1."""
        if set(self.outcomes) != set(self.market.selections):
            msg = f"{self.market} needs outcomes for {self.market.selections}, got {self.outcomes}"
            raise ModelInputError(msg)
        ordered = {selection: self.outcomes[selection] for selection in self.market.selections}
        object.__setattr__(self, "outcomes", MappingProxyType(ordered))
        if is_partition_market(self.market):
            total = math.fsum(self.win_probability(selection) for selection in ordered)
            if abs(total - 1.0) > PARTITION_SUM_TOLERANCE:
                msg = f"win probabilities of {self.market} must sum to 1, got {total!r}"
                raise ModelInputError(msg)

    def win_probability(self, selection: Selection) -> float:
        """Probability that the bet ends in profit (a full or a half win)."""
        outcome = self.outcomes[self.market.require_selection(selection)]
        return outcome.win + outcome.half_win


def is_half_line(line: float) -> bool:
    """True for lines ending in .5 (no push, no half outcomes)."""
    return round(line * _QUARTERS_PER_UNIT) % _QUARTERS_PER_UNIT == _HALF_LINE_QUARTERS


def is_partition_market(market: Market) -> bool:
    """True if exactly one selection wins in full and none can push or half-settle.

    That is the case for 1X2, BTTS and half-line totals/handicaps. These are the markets
    proper scoring rules (log-loss, Brier) and calibration are computed on.
    """
    if market.type is MarketType.DOUBLE_CHANCE:
        return False
    return market.line is None or is_half_line(market.line)


def binary_market(
    market: Market, win_probabilities: Mapping[Selection, float]
) -> MarketProbabilities:
    """A market whose selections only win or lose, from their win probabilities."""
    return MarketProbabilities(
        market,
        {
            selection: AsianOutcomeProbabilities.binary(probability)
            for selection, probability in win_probabilities.items()
        },
    )


def two_sided_market(market: Market, first: AsianOutcomeProbabilities) -> MarketProbabilities:
    """A totals/handicap market from its first side (over / home); the second is opposite."""
    first_selection, second_selection = market.selections
    return MarketProbabilities(market, {first_selection: first, second_selection: first.opposite()})


def market_from_matrix(matrix: FloatArray, market: Market) -> MarketProbabilities:
    """Probabilities of ``market`` from a score matrix ``m[home_goals, away_goals]``."""
    match market.type:
        case MarketType.MATCH_RESULT:
            result = match_result(matrix)
            return binary_market(
                market,
                {
                    Selection.HOME: result.home,
                    Selection.DRAW: result.draw,
                    Selection.AWAY: result.away,
                },
            )
        case MarketType.DOUBLE_CHANCE:
            chance = double_chance(matrix)
            return binary_market(
                market,
                {
                    Selection.HOME_OR_DRAW: chance.home_or_draw,
                    Selection.HOME_OR_AWAY: chance.home_or_away,
                    Selection.DRAW_OR_AWAY: chance.draw_or_away,
                },
            )
        case MarketType.BTTS:
            btts = both_teams_to_score(matrix)
            return binary_market(market, {Selection.YES: btts.yes, Selection.NO: btts.no})
        case MarketType.TOTALS:
            return two_sided_market(market, total_goals(matrix, _line(market)).first)
        case MarketType.ASIAN_HANDICAP:
            return two_sided_market(market, asian_handicap(matrix, _line(market)).first)


def predict_markets(
    model: DixonColesModel, home_team: str, away_team: str, markets: Iterable[Market]
) -> dict[Market, MarketProbabilities]:
    """Dixon-Coles probabilities of every requested market of one fixture.

    The score matrix is computed once and shared by all markets, so they are mutually
    consistent. Team keys follow :func:`app.models.adapters.team_key`.

    Raises:
        UnknownTeamError: a team is not in the fitted model (callers skip the fixture).
    """
    matrix = model.score_matrix(home_team, away_team)
    return {market: market_from_matrix(matrix, market) for market in dict.fromkeys(markets)}


def settle_selection(
    market: Market, selection: Selection, home_goals: int, away_goals: int
) -> Settlement:
    """Settle a bet on ``selection`` given the full-time score."""
    market.require_selection(selection)
    if min(home_goals, away_goals) < 0:
        msg = f"goals must be >= 0, got {home_goals}-{away_goals}"
        raise ModelInputError(msg)
    if market.type is MarketType.TOTALS:
        margin = home_goals + away_goals - _line(market)
        return settle(margin if selection is Selection.OVER else -margin)
    if market.type is MarketType.ASIAN_HANDICAP:
        margin = home_goals - away_goals + _line(market)
        return settle(margin if selection is Selection.HOME else -margin)
    return (
        Settlement.WIN
        if selection in _winning_selections(home_goals, away_goals)
        else Settlement.LOSS
    )


def _winning_selections(home_goals: int, away_goals: int) -> frozenset[Selection]:
    """Selections of the line-less markets that win with this score."""
    if home_goals > away_goals:
        result = {Selection.HOME, Selection.HOME_OR_DRAW, Selection.HOME_OR_AWAY}
    elif home_goals < away_goals:
        result = {Selection.AWAY, Selection.HOME_OR_AWAY, Selection.DRAW_OR_AWAY}
    else:
        result = {Selection.DRAW, Selection.HOME_OR_DRAW, Selection.DRAW_OR_AWAY}
    result.add(Selection.YES if min(home_goals, away_goals) > 0 else Selection.NO)
    return frozenset(result)


def _line(market: Market) -> float:
    # Market guarantees a line for totals/handicap; this narrows the type for mypy.
    if market.line is None:  # pragma: no cover - excluded by the domain invariant
        msg = f"market {market} has no line"
        raise ModelInputError(msg)
    return market.line
