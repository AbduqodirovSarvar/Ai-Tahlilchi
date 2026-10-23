"""From predictions to settled bets: ensemble, EV, filters, stakes and settlement.

Stakes and profits are in abstract *units* (floats) for statistics, not money: real money
amounts are ``Decimal`` and are computed by :func:`app.models.staking.stake_amount`.

Kelly stakes are a fraction of a constant bank (no compounding), so the result does not
depend on the order of same-day bets and stays comparable with flat staking.
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

from app.domain import BOOKMAKER_PINNACLE, Market, OddsKind, Selection
from app.models.asian import AsianOutcomeProbabilities, Settlement, settlement_returns
from app.models.backtest.walk_forward import FixturePrediction
from app.models.ensemble import ensemble_markets
from app.models.ev import asian_expected_value, asian_fair_odds
from app.models.exceptions import ModelInputError
from app.models.market import FairMarket
from app.models.outcomes import settle_selection
from app.models.staking import (
    DEFAULT_KELLY_MULTIPLIER,
    DEFAULT_MAX_STAKE_FRACTION,
    asian_kelly_fraction,
    capped_fraction,
)

# "Balanced" profile defaults (decisions log #8).
DEFAULT_MIN_PROBABILITY: Final = 0.45
DEFAULT_MIN_EV: Final = 0.05
DEFAULT_ODDS_MIN: Final = 1.50
DEFAULT_ODDS_MAX: Final = 3.00
DEFAULT_FLAT_UNIT: Final = 1.0
DEFAULT_BANK_UNITS: Final = 100.0


@dataclass(frozen=True, slots=True, kw_only=True)
class BetFilter:
    """Which candidate bets are placed."""

    min_probability: float = DEFAULT_MIN_PROBABILITY
    min_ev: float = DEFAULT_MIN_EV
    odds_min: float = DEFAULT_ODDS_MIN
    odds_max: float = DEFAULT_ODDS_MAX

    def __post_init__(self) -> None:
        """Require a non-empty odds range above 1 and a probability floor in [0, 1]."""
        if not 1.0 < self.odds_min < self.odds_max or not 0.0 <= self.min_probability <= 1.0:
            msg = f"invalid bet filter: {self}"
            raise ModelInputError(msg)

    def accepts(self, candidate: "Bet") -> bool:
        """True if ``candidate`` passes every threshold."""
        return (
            candidate.probability >= self.min_probability
            and candidate.ev >= self.min_ev
            and self.odds_min <= candidate.odds <= self.odds_max
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PricingConfig:
    """Where the bet price comes from (default: Pinnacle opening, the earliest sharp price)."""

    bookmaker: str = BOOKMAKER_PINNACLE
    kind: OddsKind = OddsKind.OPENING
    closing_kind: OddsKind = OddsKind.CLOSING


@dataclass(frozen=True, slots=True, kw_only=True)
class StakingConfig:
    """Flat unit and fractional-Kelly settings (units, constant bank)."""

    flat_unit: float = DEFAULT_FLAT_UNIT
    bank: float = DEFAULT_BANK_UNITS
    kelly_multiplier: float = DEFAULT_KELLY_MULTIPLIER
    max_fraction: float = DEFAULT_MAX_STAKE_FRACTION


@dataclass(frozen=True, slots=True, kw_only=True)
class Bet:
    """One candidate or placed bet, already settled.

    ``probability`` is the ensemble probability of ending in profit (win or half win);
    ``ev`` uses the full settlement distribution. ``closing_ev`` is the EV of the same bet
    under the de-vigged closing distribution and ``closing_fair_odds`` its fair price, both
    ``None`` when there is no closing quote. ``closing_odds`` is the same bookmaker's
    closing price of the selection and ``closing_profit`` the flat profit had the bet been
    struck there (the F3 gate judges ROI at closing odds).
    """

    fixture: str
    league: str
    season: int
    match_date: date
    market: Market
    selection: Selection
    odds: float
    probability: float
    ev: float
    settlement: Settlement
    flat_stake: float
    flat_profit: float
    kelly_stake: float
    kelly_profit: float
    closing_fair_odds: float | None
    closing_ev: float | None
    closing_odds: float | None
    closing_profit: float | None

    @property
    def flat_at_risk(self) -> float:
        """The flat stake that was at risk: a push returns the stake, so it counts for nothing."""
        return 0.0 if self.settlement is Settlement.PUSH else self.flat_stake

    @property
    def kelly_at_risk(self) -> float:
        """The Kelly stake that was at risk (same rule as :attr:`flat_at_risk`)."""
        return 0.0 if self.settlement is Settlement.PUSH else self.kelly_stake

    @property
    def clv(self) -> float | None:
        """Closing line value: ``log(odds taken / fair closing odds)`` (> 0 beat the close)."""
        if self.closing_fair_odds is None:
            return None
        return math.log(self.odds / self.closing_fair_odds)


def candidate_bets(
    predictions: Iterable[FixturePrediction],
    *,
    weight: float,
    pricing: PricingConfig,
    staking: StakingConfig,
) -> list[Bet]:
    """Every priced selection of every prediction, with its EV and settled result.

    The expensive part (ensembling) is done once per weight; thresholds are applied later
    by :func:`select_bets`, which keeps the parameter grid cheap.
    """
    candidates = []
    for prediction in predictions:
        mixed = ensemble_markets(prediction.model, prediction.market, weight)
        for market, probabilities in mixed.items():
            quote = prediction.item.quote(pricing.kind, pricing.bookmaker, market)
            if quote is None:
                continue
            closing_quote = prediction.item.quote(pricing.closing_kind, pricing.bookmaker, market)
            for selection in market.selections:
                pick = _Pick(
                    market=market,
                    selection=selection,
                    odds=quote.prices[selection],
                    closing_odds=None if closing_quote is None else closing_quote.prices[selection],
                )
                candidates.append(
                    _candidate(
                        prediction,
                        pick,
                        probabilities.outcomes[selection],
                        prediction.closing.get(market),
                        staking,
                    )
                )
    return candidates


def select_bets(candidates: Sequence[Bet], bet_filter: BetFilter) -> list[Bet]:
    """At most one bet per fixture: the highest-EV candidate that passes the filter."""
    best: dict[str, Bet] = {}
    for candidate in candidates:
        if not bet_filter.accepts(candidate):
            continue
        current = best.get(candidate.fixture)
        if current is None or candidate.ev > current.ev:
            best[candidate.fixture] = candidate
    return sorted(best.values(), key=lambda bet: (bet.match_date, bet.fixture))


@dataclass(frozen=True, slots=True, kw_only=True)
class _Pick:
    market: Market
    selection: Selection
    odds: float
    closing_odds: float | None


def _candidate(
    prediction: FixturePrediction,
    pick: _Pick,
    outcomes: AsianOutcomeProbabilities,
    closing: FairMarket | None,
    staking: StakingConfig,
) -> Bet:
    item = prediction.item
    settlement = settle_selection(
        pick.market, pick.selection, item.record.home_goals, item.record.away_goals
    )
    unit_return = settlement_returns(pick.odds)[settlement]
    ev = asian_expected_value(outcomes, pick.odds)
    fraction = capped_fraction(
        asian_kelly_fraction(outcomes, pick.odds),
        multiplier=staking.kelly_multiplier,
        max_fraction=staking.max_fraction,
    )
    kelly_stake = fraction * staking.bank
    flat_stake = staking.flat_unit
    closing_outcomes = None if closing is None else closing.probabilities.outcomes[pick.selection]
    return Bet(
        fixture=item.key,
        league=item.league_name,
        season=item.season,
        match_date=item.match_date,
        market=pick.market,
        selection=pick.selection,
        odds=pick.odds,
        probability=outcomes.win + outcomes.half_win,
        ev=ev,
        settlement=settlement,
        flat_stake=flat_stake,
        flat_profit=flat_stake * unit_return,
        kelly_stake=kelly_stake,
        kelly_profit=kelly_stake * unit_return,
        closing_fair_odds=_fair_odds_or_none(closing_outcomes),
        closing_ev=(
            None if closing_outcomes is None else asian_expected_value(closing_outcomes, pick.odds)
        ),
        closing_odds=pick.closing_odds,
        closing_profit=(
            None
            if pick.closing_odds is None
            else flat_stake * settlement_returns(pick.closing_odds)[settlement]
        ),
    )


def _fair_odds_or_none(outcomes: AsianOutcomeProbabilities | None) -> float | None:
    if outcomes is None or outcomes.win + outcomes.half_win <= 0.0:
        return None
    return asian_fair_odds(outcomes)
