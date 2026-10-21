"""Strict walk-forward prediction: every fixture is predicted only from its past.

For each league the fixtures to evaluate are visited in date order. The Dixon-Coles model
is refitted when the fixture date is ``refit_days`` or more after the last fit; a fit with
``as_of = d`` uses only matches whose model date is strictly before ``d`` (and within
``max_history_days``), so nothing played on the prediction day or later can leak in. The
fitter itself also rejects any match on or after ``as_of`` (ADR 0003), and a test
instruments every fit to prove it.

Market probabilities at prediction time come from ``prediction_kind`` prices (opening by
default); closing prices are used only for the closing baseline and CLV, never to decide a
bet.
"""

from bisect import bisect_left
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from app.domain import Market, OddsKind
from app.models.backtest.dataset import BacktestFixture
from app.models.confidence import MIN_TEAM_MATCHES
from app.models.exceptions import ModelFitError, ModelInputError, UnknownTeamError
from app.models.market import DEFAULT_MARKET_MODEL, FairMarket, MarketModelConfig, fair_markets
from app.models.outcomes import MarketProbabilities, predict_markets
from app.models.poisson import DEFAULT_XI_PER_DAY, DixonColesModel, MatchRecord, fit_dixon_coles

DEFAULT_REFIT_DAYS: Final = 7
# About three seasons; older matches carry little weight at any sensible xi.
DEFAULT_MAX_HISTORY_DAYS: Final = 1100


@dataclass(frozen=True, slots=True, kw_only=True)
class WalkForwardConfig:
    """Model and data settings of one walk-forward run."""

    xi: float = DEFAULT_XI_PER_DAY
    refit_days: int = DEFAULT_REFIT_DAYS
    max_history_days: int = DEFAULT_MAX_HISTORY_DAYS
    min_team_matches: int = MIN_TEAM_MATCHES
    prediction_kind: OddsKind = OddsKind.OPENING
    closing_kind: OddsKind = OddsKind.CLOSING
    market_model: MarketModelConfig = DEFAULT_MARKET_MODEL

    def __post_init__(self) -> None:
        """Reject settings that cannot produce a meaningful run."""
        if self.refit_days < 1 or self.max_history_days < 1 or self.min_team_matches < 0:
            msg = f"refit/history days must be >= 1 and min_team_matches >= 0: {self}"
            raise ModelInputError(msg)
        if self.prediction_kind is self.closing_kind:
            msg = "prediction prices must not be the closing prices (look-ahead)"
            raise ModelInputError(msg)


class SkipReason(StrEnum):
    """Why a fixture to evaluate got no prediction."""

    FIT_FAILED = "fit_failed"
    THIN_HISTORY = "thin_history"
    UNKNOWN_TEAM = "unknown_team"
    NO_PRICES = "no_prices"


@dataclass(frozen=True, slots=True)
class FixturePrediction:
    """Everything known about one fixture at prediction time, plus its closing baseline."""

    item: BacktestFixture
    as_of: date
    team_matches: int
    model: Mapping[Market, MarketProbabilities]
    market: Mapping[Market, FairMarket]
    closing: Mapping[Market, FairMarket]


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    """Predictions in date order and bookkeeping of what could not be predicted."""

    predictions: tuple[FixturePrediction, ...]
    skipped: Mapping[SkipReason, int] = field(default_factory=lambda: MappingProxyType({}))
    fits: int = 0
    fit_failures: int = 0


@dataclass(slots=True)
class _Fit:
    """The model currently in use for a league (``None`` if its fit failed)."""

    as_of: date
    model: DixonColesModel | None
    team_matches: Counter[str]


def walk_forward(
    dataset: Sequence[BacktestFixture], config: WalkForwardConfig, *, seasons: Collection[int]
) -> WalkForwardResult:
    """Predict every fixture of ``seasons`` from strictly earlier matches of its league."""
    predictions: list[FixturePrediction] = []
    skipped: Counter[SkipReason] = Counter()
    fits = failures = 0
    for league_items in _by_league(dataset).values():
        dates = [item.match_date for item in league_items]
        current: _Fit | None = None
        for item in league_items:
            if item.season not in seasons:
                continue
            if current is None or (item.match_date - current.as_of).days >= config.refit_days:
                current = _fit(league_items, dates, item.match_date, config)
                fits += 1
                failures += current.model is None
            outcome = _predict(item, current, config)
            if isinstance(outcome, SkipReason):
                skipped[outcome] += 1
            else:
                predictions.append(outcome)
    predictions.sort(key=lambda prediction: (prediction.item.fixture.kickoff, prediction.item.key))
    return WalkForwardResult(
        predictions=tuple(predictions),
        skipped=MappingProxyType(dict(skipped)),
        fits=fits,
        fit_failures=failures,
    )


def _by_league(dataset: Sequence[BacktestFixture]) -> dict[str, list[BacktestFixture]]:
    leagues: dict[str, list[BacktestFixture]] = {}
    for item in dataset:
        leagues.setdefault(item.league, []).append(item)
    for items in leagues.values():
        items.sort(key=lambda item: (item.match_date, item.key))
    return leagues


def _fit(
    league_items: Sequence[BacktestFixture],
    dates: Sequence[date],
    as_of: date,
    config: WalkForwardConfig,
) -> _Fit:
    """Fit on the league's matches in ``[as_of - max_history_days, as_of)``."""
    start = bisect_left(dates, as_of - timedelta(days=config.max_history_days))
    stop = bisect_left(dates, as_of)
    history: list[MatchRecord] = [item.record for item in league_items[start:stop]]
    counts: Counter[str] = Counter()
    for record in history:
        counts[record.home_team] += 1
        counts[record.away_team] += 1
    try:
        model = fit_dixon_coles(history, as_of, xi=config.xi)
    except (ModelInputError, ModelFitError):
        model = None
    return _Fit(as_of=as_of, model=model, team_matches=counts)


def _predict(
    item: BacktestFixture, fit: _Fit, config: WalkForwardConfig
) -> FixturePrediction | SkipReason:
    if fit.model is None:
        return SkipReason.FIT_FAILED
    team_matches = min(fit.team_matches[item.home], fit.team_matches[item.away])
    if team_matches < config.min_team_matches:
        return SkipReason.THIN_HISTORY
    markets = [quote.market for quote in item.quotes if quote.kind is config.prediction_kind]
    if not markets:
        return SkipReason.NO_PRICES
    try:
        model = predict_markets(fit.model, item.home, item.away, markets)
    except UnknownTeamError:
        return SkipReason.UNKNOWN_TEAM
    market_config = config.market_model
    return FixturePrediction(
        item=item,
        as_of=fit.as_of,
        team_matches=team_matches,
        model=MappingProxyType(model),
        market=MappingProxyType(
            fair_markets(
                item.quotes, kind=config.prediction_kind, config=market_config, references=model
            )
        ),
        closing=MappingProxyType(
            fair_markets(
                item.quotes, kind=config.closing_kind, config=market_config, references=model
            )
        ),
    )
