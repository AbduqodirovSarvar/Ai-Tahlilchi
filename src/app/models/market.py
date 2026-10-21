"""Market model (F3.3): fair probabilities from a sharp bookmaker's de-vigged prices.

Pinnacle's prices are the best public estimate of the true probabilities (low margin, high
limits, sharp bettors move the line). The market model removes the margin from them; when
the preferred bookmaker has no complete quote it falls back to the market average and flags
the result as more uncertain (an average of soft books is noisier and carries more margin).

Asian whole and quarter lines (e.g. AH -1.0, total 2.25) need extra care: two prices cannot
pin down five settlement outcomes. The *middle* masses (push, half win, half loss) are taken
from a reference distribution (in practice the Dixon-Coles one) and only the decisive
win/loss split is solved from the prices. This is exact for whole lines and keeps the
"both sides fair" condition for quarter lines (derivation in ADR 0005).
"""

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from app.domain import (
    BOOKMAKER_MARKET_AVERAGE,
    BOOKMAKER_PINNACLE,
    Market,
    MarketType,
    OddsKind,
    Selection,
)
from app.models._validation import check_probability
from app.models.adapters import MarketQuote
from app.models.asian import AsianOutcomeProbabilities
from app.models.devig import DevigMethod, devig, implied_probabilities, overround
from app.models.exceptions import ModelInputError
from app.models.outcomes import (
    MarketProbabilities,
    binary_market,
    is_half_line,
    two_sided_market,
)

DEFAULT_BOOKMAKER_PRIORITY: Final = (BOOKMAKER_PINNACLE,)
DEFAULT_FALLBACK_BOOKMAKERS: Final = (BOOKMAKER_MARKET_AVERAGE,)
# Every match outcome belongs to exactly two of the three double-chance selections.
DOUBLE_CHANCE_TOTAL: Final = 2.0


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketModelConfig:
    """Which bookmakers the market model trusts, in order, and how it removes the margin."""

    priority: tuple[str, ...] = DEFAULT_BOOKMAKER_PRIORITY
    fallback: tuple[str, ...] = DEFAULT_FALLBACK_BOOKMAKERS
    method: DevigMethod = DevigMethod.SHIN

    def __post_init__(self) -> None:
        """Require at least one preferred bookmaker and disjoint lists."""
        if not self.priority:
            msg = "the market model needs at least one preferred bookmaker"
            raise ModelInputError(msg)
        if set(self.priority) & set(self.fallback):
            msg = f"a bookmaker cannot be both preferred and fallback: {self}"
            raise ModelInputError(msg)


DEFAULT_MARKET_MODEL: Final = MarketModelConfig()


@dataclass(frozen=True, slots=True, kw_only=True)
class FairMarket:
    """Fair (margin-free) probabilities of one market and where they came from.

    ``wide_uncertainty`` is set when they come from a fallback bookmaker. ``derived`` marks
    a market computed from another one (double chance from 1X2), not from its own prices.
    """

    probabilities: MarketProbabilities
    bookmaker: str
    kind: OddsKind
    overround: float
    wide_uncertainty: bool
    derived: bool = False

    @property
    def market(self) -> Market:
        """The market these probabilities describe."""
        return self.probabilities.market


def asian_from_two_way(
    first_probability: float, reference: AsianOutcomeProbabilities
) -> AsianOutcomeProbabilities:
    """Settlement distribution of the first side of a two-way Asian market.

    ``first_probability`` is the de-vigged probability of the first side (over / home), so
    its fair odds are ``1 / first_probability``. The push and half masses are taken from
    ``reference``; the win/loss split is the one at which both sides are fair. When the
    prices are too extreme for the reference's middle mass, that mass is scaled down just
    enough to keep every probability non-negative.
    """
    p_first = check_probability(first_probability)
    p_second = 1.0 - p_first
    half_win, push, half_loss = reference.half_win, reference.push, reference.half_loss
    middle_weight = push + (half_win + half_loss) / 2.0
    scale = min(
        1.0,
        _middle_scale_limit(p_first, middle_weight, half_win),
        _middle_scale_limit(p_second, middle_weight, half_loss),
    )
    decisive = 1.0 - scale * middle_weight
    values = (
        max(p_first * decisive - scale * half_win / 2.0, 0.0),
        scale * half_win,
        scale * push,
        scale * half_loss,
        max(p_second * decisive - scale * half_loss / 2.0, 0.0),
    )
    total = math.fsum(values)
    return AsianOutcomeProbabilities(*(value / total for value in values))


def _middle_scale_limit(probability: float, middle_weight: float, half_mass: float) -> float:
    """Largest middle-mass scale keeping ``probability * (1 - t*c) - t*h/2 >= 0``."""
    denominator = probability * middle_weight + half_mass / 2.0
    return math.inf if denominator <= 0.0 else probability / denominator


def devig_quote(
    quote: MarketQuote,
    *,
    method: DevigMethod = DevigMethod.SHIN,
    reference: MarketProbabilities | None = None,
) -> MarketProbabilities:
    """Fair probabilities of one complete quote.

    Raises:
        ModelInputError: an Asian whole/quarter line without a ``reference`` distribution
            for its push and half outcomes, or a reference for another market.
    """
    market = quote.market
    prices = quote.ordered_prices()
    if market.type is MarketType.DOUBLE_CHANCE:
        return _devig_double_chance(market, prices)
    fair = devig(prices, method)
    if market.line is None or is_half_line(market.line):
        return binary_market(market, dict(zip(market.selections, fair, strict=True)))
    if reference is None or reference.market != market:
        msg = f"{market} has push/half outcomes; a reference distribution for it is required"
        raise ModelInputError(msg)
    first = market.selections[0]
    return two_sided_market(market, asian_from_two_way(fair[0], reference.outcomes[first]))


def _devig_double_chance(market: Market, prices: tuple[float, ...]) -> MarketProbabilities:
    """Proportional de-vig to a total of 2 (the selections overlap; Shin does not apply)."""
    implied = implied_probabilities(prices)
    scale = DOUBLE_CHANCE_TOTAL / math.fsum(implied)
    return binary_market(
        market,
        {
            selection: min(value * scale, 1.0)
            for selection, value in zip(market.selections, implied, strict=True)
        },
    )


def double_chance_from_match_result(result: MarketProbabilities) -> MarketProbabilities:
    """Double-chance probabilities implied by a 1X2 distribution."""
    if result.market.type is not MarketType.MATCH_RESULT:
        msg = f"expected a 1X2 market, got {result.market}"
        raise ModelInputError(msg)
    home = result.win_probability(Selection.HOME)
    draw = result.win_probability(Selection.DRAW)
    away = result.win_probability(Selection.AWAY)
    return binary_market(
        Market.double_chance(),
        {
            Selection.HOME_OR_DRAW: home + draw,
            Selection.HOME_OR_AWAY: home + away,
            Selection.DRAW_OR_AWAY: draw + away,
        },
    )


def fair_markets(
    quotes: Iterable[MarketQuote],
    *,
    kind: OddsKind,
    config: MarketModelConfig = DEFAULT_MARKET_MODEL,
    references: Mapping[Market, MarketProbabilities] | None = None,
) -> dict[Market, FairMarket]:
    """Fair probabilities of every market of one fixture priced at moment ``kind``.

    Per market the first bookmaker of ``config.priority`` with a complete quote is used,
    else the first of ``config.fallback`` (flagged ``wide_uncertainty``); other bookmakers
    (e.g. the market maximum, which has no margin) are ignored. Asian whole/quarter lines
    without a reference distribution in ``references`` are skipped. Double chance is
    derived from 1X2 when it has no quote of its own.

    Raises:
        ModelInputError: the quotes belong to more than one fixture.
    """
    by_market = _quotes_by_market(quotes, kind)
    fair: dict[Market, FairMarket] = {}
    for market, by_bookmaker in by_market.items():
        reference = None if references is None else references.get(market)
        chosen = _choose_quote(by_bookmaker, config)
        if chosen is None or _needs_missing_reference(market, reference):
            continue
        quote, wide = chosen
        fair[market] = FairMarket(
            probabilities=devig_quote(quote, method=config.method, reference=reference),
            bookmaker=quote.bookmaker,
            kind=kind,
            overround=overround(quote.ordered_prices()),
            wide_uncertainty=wide,
        )
    _add_derived_double_chance(fair)
    return fair


def _quotes_by_market(
    quotes: Iterable[MarketQuote], kind: OddsKind
) -> dict[Market, dict[str, MarketQuote]]:
    by_market: dict[Market, dict[str, MarketQuote]] = {}
    fixtures = set()
    for quote in quotes:
        fixtures.add(quote.fixture)
        if quote.kind is kind:
            by_market.setdefault(quote.market, {})[quote.bookmaker] = quote
    if len(fixtures) > 1:
        msg = f"quotes of one fixture expected, got {len(fixtures)} fixtures"
        raise ModelInputError(msg)
    return by_market


def _choose_quote(
    by_bookmaker: Mapping[str, MarketQuote], config: MarketModelConfig
) -> tuple[MarketQuote, bool] | None:
    """The quote to trust and whether it is a fallback (wider uncertainty)."""
    for bookmakers, wide in ((config.priority, False), (config.fallback, True)):
        for bookmaker in bookmakers:
            if bookmaker in by_bookmaker:
                return by_bookmaker[bookmaker], wide
    return None


def _needs_missing_reference(market: Market, reference: MarketProbabilities | None) -> bool:
    has_middle = market.line is not None and not is_half_line(market.line)
    return has_middle and reference is None


def _add_derived_double_chance(fair: dict[Market, FairMarket]) -> None:
    result = fair.get(Market.match_result())
    if result is None or Market.double_chance() in fair:
        return
    fair[Market.double_chance()] = FairMarket(
        probabilities=double_chance_from_match_result(result.probabilities),
        bookmaker=result.bookmaker,
        kind=result.kind,
        overround=result.overround,
        wide_uncertainty=result.wide_uncertainty,
        derived=True,
    )
