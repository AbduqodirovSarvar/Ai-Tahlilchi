"""From a fixture, its prices and a fitted league to candidate selections (pure, no I/O).

This is the F4.2 calculation chain, expressed only in ``app.models`` functions:

1. Dixon-Coles probabilities of every market the user's bookmaker prices;
2. fair probabilities from Pinnacle's de-vigged prices (``market_avg`` as a flagged fallback);
3. the ensemble ``w * model + (1 - w) * market``;
4. EV against the *user's bookmaker's* price (decision #10), Asian half outcomes included;
5. the confidence level - ``HIGH`` stays out of reach while neither expected goals nor
   line-ups are known (the model's own rule), and a model/market gap above 15 points marks
   the selection as "check me" instead of "value".

Markets without a market anchor are skipped unless ``allow_model_only`` (custom analysis,
where a model-only number is shown with the lowest trust). Nothing here knows about users,
thresholds or the database.

Prices are only compared when they are close in time: the user's bookmaker quote must be
younger than ``config.max_quote_age`` (otherwise the fixture is skipped as ``stale_price``),
and an anchor quote taken more than ``config.max_quote_skew`` away from it is not used (the
market then has no anchor), so a fresh 1xBet price is never judged against an old Pinnacle
one or the other way round.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Final

from app.domain import Fixture, Market, MarketType, Selection
from app.models import (
    AsianOutcomeProbabilities,
    ConfidenceAssessment,
    ConfidenceInputs,
    ConfidenceLevel,
    FairMarket,
    MarketProbabilities,
    MarketQuote,
    ModelInputError,
    UnknownTeamError,
    asian_expected_value,
    asian_fair_odds,
    asian_kelly_fraction,
    assess_confidence,
    capped_fraction,
    ensemble_markets,
    fair_markets,
    predict_markets,
    stake_amount,
    team_key,
)
from app.models.calibration import (
    CalibrationVersion,
    apply_calibration,
    calibrate_complementary,
    rescale_outcomes,
)
from app.pipeline.config import PipelineConfig, StakingMode
from app.pipeline.modeling import FittedLeague
from app.pipeline.odds import CURRENT_KIND

SECONDS_PER_DAY: Final = 86_400.0
PARTITION_TOLERANCE: Final = 1e-6
"""A market whose selections sum to one within this is calibrated as a whole."""
KELLY_UNIT_STEP: Final = Decimal("0.1")
"""Kelly stakes are rounded down to tenths of a unit."""

FLAG_MODEL_MARKET_GAP: Final = "model_market_gap"
FLAG_NO_MARKET_REFERENCE: Final = "no_market_reference"

SKIP_NO_PRICE: Final = "no_price"
SKIP_STALE_PRICE: Final = "stale_price"
SKIP_NO_MODEL: Final = "no_model"
SKIP_THIN_HISTORY: Final = "thin_history"
SKIP_UNKNOWN_TEAM: Final = "unknown_team"
SKIP_MODEL_ERROR: Final = "model_error"


@dataclass(frozen=True, slots=True, kw_only=True)
class Candidate:
    """One priced selection with its numbers.

    ``probability`` is the ensemble probability of ending in profit (win or half win);
    ``outcomes`` the full settlement distribution EV and Kelly are computed from.
    """

    market: Market
    selection: Selection
    odds: float
    probability: float
    model_probability: float
    market_probability: float | None
    ev: float
    outcomes: AsianOutcomeProbabilities
    assessment: ConfidenceAssessment
    anchored: bool
    quoted_at: datetime
    """When the user's bookmaker price was taken (the card's ``data_as_of``)."""
    raw_probability: float | None = None
    """The probability before the active calibration (``None`` = ``probability`` is raw)."""

    @property
    def fair_odds(self) -> float | None:
        """Odds at which the selection has zero EV for the ensemble (``None`` if undefined)."""
        try:
            fair = asian_fair_odds(self.outcomes)
        except ModelInputError:
            return None
        return fair if fair > 1.0 else None

    @property
    def confidence(self) -> ConfidenceLevel:
        """The confidence level (HIGH/MEDIUM/LOW)."""
        return self.assessment.level

    @property
    def risk_flags(self) -> tuple[str, ...]:
        """Stable codes for what makes the number less trustworthy."""
        flags = []
        if self.assessment.sanity_flag:
            flags.append(FLAG_MODEL_MARKET_GAP)
        if not self.anchored:
            flags.append(FLAG_NO_MARKET_REFERENCE)
        return tuple(flags)


@dataclass(frozen=True, slots=True, kw_only=True)
class Valuation:
    """The candidates of one fixture, or why there are none (``skip`` is a ``SKIP_*`` code)."""

    candidates: tuple[Candidate, ...] = ()
    skip: str | None = None
    min_team_matches: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class ValuationInput:
    """Everything :func:`value_fixture` needs about one fixture."""

    fixture: Fixture
    quotes: Sequence[MarketQuote]
    fitted: FittedLeague
    now: datetime
    markets: frozenset[MarketType]
    lineups_known: bool = False
    allow_model_only: bool = False
    require_fresh: bool = True
    """Refuse prices older than ``max_quote_age``; off for a look-back at a finished match."""
    calibration: CalibrationVersion | None = None
    """The active probability calibration of the learning loop (``None`` = none)."""


def _bet_quotes(item: ValuationInput, config: PipelineConfig) -> dict[Market, MarketQuote] | str:
    """The user's bookmaker's fresh quotes by market, or the ``SKIP_*`` code why there are none."""
    priced = {
        quote.market: quote
        for quote in item.quotes
        if quote.bookmaker == config.bet_bookmaker and quote.market.type in item.markets
    }
    if not priced:
        return SKIP_NO_PRICE
    fresh = {
        market: quote
        for market, quote in priced.items()
        if not item.require_fresh or item.now - quote.taken_at <= config.max_quote_age
    }
    return fresh or SKIP_STALE_PRICE


def value_fixture(item: ValuationInput, config: PipelineConfig) -> Valuation:
    """Candidates for every selection the user's bookmaker prices (see module docstring)."""
    bet_quotes = _bet_quotes(item, config)
    if isinstance(bet_quotes, str):
        return Valuation(skip=bet_quotes)
    model = item.fitted.model
    if model is None:
        return Valuation(skip=SKIP_NO_MODEL)
    home, away = team_key(item.fixture.home), team_key(item.fixture.away)
    played = min(item.fitted.team_matches.get(home, 0), item.fitted.team_matches.get(away, 0))
    if played < config.min_team_matches:
        return Valuation(skip=SKIP_THIN_HISTORY, min_team_matches=played)
    try:
        predicted = predict_markets(model, home, away, list(bet_quotes))
    except UnknownTeamError:
        return Valuation(skip=SKIP_UNKNOWN_TEAM, min_team_matches=played)
    except ModelInputError:
        return Valuation(skip=SKIP_MODEL_ERROR, min_team_matches=played)
    fair = _fair(_aligned(item.quotes, bet_quotes, config), predicted)
    ensembled = ensemble_markets(predicted, fair, config.model_weight)
    candidates: list[Candidate] = []
    for market, quote in bet_quotes.items():
        probabilities = ensembled.get(market)
        if probabilities is None:
            if not item.allow_model_only:
                continue
            probabilities = predicted[market]
        candidates.extend(
            _candidates(
                quote,
                probabilities,
                predicted[market],
                fair.get(market),
                (item, played),
            )
        )
    return Valuation(
        candidates=tuple(calibrate_candidates(candidates, item.calibration)),
        min_team_matches=played,
    )


def calibrate_candidates(
    candidates: Sequence[Candidate], calibration: CalibrationVersion | None
) -> list[Candidate]:
    """``candidates`` with their probabilities (and EV) replaced by the calibrated ones.

    The selections of a market that partition the result (their probabilities sum to one, e.g.
    1X2) are calibrated together and renormalised; any other selection (Asian lines with a
    push, partial markets) one by one. The raw probability is kept for the next training run.
    """
    if calibration is None:
        return list(candidates)
    by_market: dict[Market, list[Candidate]] = {}
    for candidate in candidates:
        by_market.setdefault(candidate.market, []).append(candidate)
    result: list[Candidate] = []
    for group in by_market.values():
        probabilities = [c.probability for c in group]
        whole = len(group) == len(group[0].market.selections) and (
            abs(math.fsum(probabilities) - 1.0) <= PARTITION_TOLERANCE
        )
        calibrated = (
            calibrate_complementary(calibration, probabilities)
            if whole
            else [apply_calibration(calibration, p) for p in probabilities]
        )
        result.extend(_recalibrated(c, p) for c, p in zip(group, calibrated, strict=True))
    return result


def _recalibrated(candidate: Candidate, probability: float) -> Candidate:
    outcomes = rescale_outcomes(candidate.outcomes, probability)
    return replace(
        candidate,
        probability=probability,
        raw_probability=candidate.probability,
        outcomes=outcomes,
        ev=asian_expected_value(outcomes, candidate.odds),
    )


def _aligned(
    quotes: Sequence[MarketQuote], bet_quotes: Mapping[Market, MarketQuote], config: PipelineConfig
) -> list[MarketQuote]:
    """Quotes the fair price may be built from: the anchors close in time to the bet quote."""
    aligned = []
    for quote in quotes:
        bet = bet_quotes.get(quote.market)
        if bet is None:
            continue
        if (
            quote.bookmaker == config.bet_bookmaker
            or abs(quote.taken_at - bet.taken_at) <= config.max_quote_skew
        ):
            aligned.append(quote)
    return aligned


def _fair(
    quotes: Sequence[MarketQuote], predicted: Mapping[Market, MarketProbabilities]
) -> dict[Market, FairMarket]:
    try:
        return fair_markets(quotes, kind=CURRENT_KIND, references=predicted)
    except ModelInputError:
        return {}


def _candidates(
    quote: MarketQuote,
    probabilities: MarketProbabilities,
    model: MarketProbabilities,
    fair: FairMarket | None,
    context: tuple[ValuationInput, int],
) -> list[Candidate]:
    item, played = context
    days = (item.fixture.kickoff - item.now).total_seconds() / SECONDS_PER_DAY
    result = []
    for selection in quote.market.selections:
        probability = _clamp(probabilities.win_probability(selection))
        if probability <= 0.0:
            continue
        outcomes = probabilities.outcomes[selection]
        market_probability = None if fair is None else fair.probabilities.win_probability(selection)
        model_probability = _clamp(model.win_probability(selection))
        assessment = assess_confidence(
            ConfidenceInputs(
                min_team_matches=played,
                xg_available=False,
                lineups_known=item.lineups_known,
                model_probability=model_probability,
                market_probability=None
                if market_probability is None
                else _clamp(market_probability),
                days_to_kickoff=days,
                market_wide_uncertainty=fair is not None and fair.wide_uncertainty,
            )
        )
        odds = quote.prices[selection]
        result.append(
            Candidate(
                market=quote.market,
                selection=selection,
                odds=odds,
                probability=probability,
                model_probability=model_probability,
                market_probability=market_probability,
                ev=asian_expected_value(outcomes, odds),
                outcomes=outcomes,
                assessment=assessment,
                anchored=fair is not None,
                quoted_at=quote.taken_at,
            )
        )
    return result


def _clamp(probability: float) -> float:
    return min(max(probability, 0.0), 1.0)


def stake_units(
    candidate: Candidate,
    config: PipelineConfig,
    *,
    bank: Decimal | None,
    unit: Decimal | None,
) -> Decimal:
    """Units to stake: nothing without a positive EV; flat, or fractional Kelly with a bank.

    Kelly uses ``capped_fraction`` (a quarter of full Kelly, at most 2% of the bank) and is
    expressed in the user's units, rounded down to tenths; flat staking is ``flat_unit``.
    """
    if candidate.ev <= 0.0:
        return Decimal(0)
    if config.staking is StakingMode.KELLY and bank is not None and unit is not None:
        fraction = capped_fraction(asian_kelly_fraction(candidate.outcomes, candidate.odds))
        return (stake_amount(bank, fraction) / unit).quantize(KELLY_UNIT_STEP, rounding=ROUND_DOWN)
    return config.flat_unit
