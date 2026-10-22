"""T4 QA (1): a tiny league with fixed ratings, every number computed independently.

The Dixon-Coles model is written down by hand (no fit): two teams, fixed attack/defence,
intercept, home advantage and rho. The oracle below recomputes the score grid with
``scipy.stats.poisson`` and the textbook tau correction, then derives probabilities, the
ensemble with a margin-free Pinnacle price, the fair price, EV and the stake (flat, and a
quarter of full Kelly capped at 2%, full Kelly found with ``minimize_scalar``). None of it
calls the code under test. The numbers are then compared with what the real services store
and return, for 1X2 picks and a quarter-line Asian handicap pick.
"""

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from types import MappingProxyType

import numpy as np
import pytest
from scipy.optimize import minimize_scalar
from scipy.stats import poisson

from app.domain import (
    BOOKMAKER_1XBET,
    BOOKMAKER_PINNACLE,
    HistoryBatch,
    Market,
    MarketType,
    OddsKind,
    Selection,
)
from app.models import DixonColesModel, MarketQuote, team_key
from app.pipeline.config import PipelineConfig, StakingMode
from app.pipeline.modeling import FittedLeague
from app.pipeline.valuation import ValuationInput, stake_units, value_fixture
from app.services.interfaces import AnalysisQuery, DateSpec, SignalView
from tests.integration.t4_support import (
    USER,
    Bare,
    make_fixture,
    open_bare,
    snapshot,
    utc,
    x12,
)

INTERCEPT, HOME_ADVANTAGE, RHO = 0.1, 0.25, -0.06
ATTACK = {"H": 0.30, "A": -0.20}
DEFENCE = {"H": 0.05, "A": 0.10}
MODEL_WEIGHT = 0.5
KICKOFF = utc(2026, 10, 21, 15)
MORNING = utc(2026, 10, 21, 5)
MIN_PROBABILITY = 0.30
ONE_X_TWO = Market.match_result()
HANDICAP = Market.asian_handicap(-0.25)
BANK, UNIT = Decimal(1000), Decimal(10)
KELLY_MULTIPLIER, STAKE_CAP = 0.25, 0.02
GRID_SIZE = 40
FIXTURE = make_fixture("Hand", KICKOFF)


# ------------------------------------------------------------------------------ the oracle
def grid() -> np.ndarray:
    """Joint score probabilities [home goals, away goals] with the textbook tau correction."""
    home_rate = np.exp(INTERCEPT + HOME_ADVANTAGE + ATTACK["H"] - DEFENCE["A"])
    away_rate = np.exp(INTERCEPT + ATTACK["A"] - DEFENCE["H"])
    goals = np.arange(GRID_SIZE)
    matrix = np.outer(poisson.pmf(goals, home_rate), poisson.pmf(goals, away_rate))
    matrix[0, 0] *= 1 - home_rate * away_rate * RHO
    matrix[0, 1] *= 1 + home_rate * RHO
    matrix[1, 0] *= 1 + away_rate * RHO
    matrix[1, 1] *= 1 - RHO
    return matrix / matrix.sum()


def goal_difference() -> np.ndarray:
    """Home goals minus away goals for every cell of the grid."""
    return np.subtract.outer(np.arange(GRID_SIZE), np.arange(GRID_SIZE))


def model_1x2() -> dict[Selection, float]:
    matrix, diff = grid(), goal_difference()
    return {
        Selection.HOME: float(matrix[diff > 0].sum()),
        Selection.DRAW: float(matrix[diff == 0].sum()),
        Selection.AWAY: float(matrix[diff < 0].sum()),
    }


# Pinnacle prices without any margin: the fair probabilities are 1/odds whatever the
# de-vig method is, so the oracle does not need to know which one the code uses.
PINNACLE = x12(2.0, 4.0, 4.0)
FAIR = {selection: 1.0 / odds for selection, odds in PINNACLE.items()}


def ensemble_1x2() -> dict[Selection, float]:
    model = model_1x2()
    return {s: MODEL_WEIGHT * model[s] + (1 - MODEL_WEIGHT) * FAIR[s] for s in model}


def asian_distribution(home_line: float, *, home_side: bool) -> dict[str, float]:
    """win / half_win / push / half_loss / loss of a (quarter) handicap, by splitting the stake."""
    quarter = (round(home_line * 4) % 2) != 0
    parts = [home_line - 0.25, home_line + 0.25] if quarter else [home_line]
    diff = goal_difference().astype(float)
    adjusted = [(diff + p) if home_side else (-diff - p) for p in parts]
    # +1 win, 0 push, -1 loss for every part; the halves add up to the five outcomes
    total = sum(np.sign(a) for a in adjusted) / len(parts)
    matrix = grid()
    names = {1.0: "win", 0.5: "half_win", 0.0: "push", -0.5: "half_loss", -1.0: "loss"}
    return {name: float(matrix[np.isclose(total, value)].sum()) for value, name in names.items()}


def asian_ev(distribution: dict[str, float], odds: float) -> float:
    return (
        distribution["win"] * (odds - 1)
        + distribution["half_win"] * (odds - 1) / 2
        - distribution["half_loss"] * 0.5
        - distribution["loss"]
    )


def asian_fair_odds(distribution: dict[str, float]) -> float:
    """The odds at which EV is zero (solved by hand from the linear EV formula)."""
    gain = distribution["win"] + distribution["half_win"] / 2
    return 1 + (distribution["half_loss"] / 2 + distribution["loss"]) / gain


def full_kelly(distribution: dict[str, float], odds: float) -> float:
    returns = {
        "win": odds - 1,
        "half_win": (odds - 1) / 2,
        "push": 0.0,
        "half_loss": -0.5,
        "loss": -1.0,
    }

    def growth(fraction: float) -> float:
        return -sum(p * np.log(1 + fraction * returns[k]) for k, p in distribution.items() if p > 0)

    best = minimize_scalar(growth, bounds=(0.0, 0.99), method="bounded", options={"xatol": 1e-12})
    return float(best.x)


def kelly_units(fraction_of_full: float) -> Decimal:
    """Bank 1000, unit 10: the quarter-Kelly money, capped at 2%, floored to cents, in tenths."""
    fraction = min(KELLY_MULTIPLIER * fraction_of_full, STAKE_CAP)
    money = (BANK * Decimal(repr(fraction))).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    return (money / UNIT).quantize(Decimal("0.1"), rounding=ROUND_DOWN)


# ------------------------------------------------------------------------------ the system
@dataclass(frozen=True)
class Prices:
    """1xBet's prices in one scenario."""

    home: float
    draw: float
    away: float
    ah_home: float
    ah_away: float


WORLD_A = Prices(home=2.10, draw=4.40, away=4.00, ah_home=1.95, ah_away=2.30)  # the cap binds
WORLD_B = Prices(home=1.95, draw=4.40, away=4.00, ah_home=1.58, ah_away=2.30)  # it does not


def hand_model() -> DixonColesModel:
    teams = {"H": team_key(FIXTURE.home), "A": team_key(FIXTURE.away)}
    return DixonColesModel(
        attack=MappingProxyType({teams[k]: v for k, v in ATTACK.items()}),
        defence=MappingProxyType({teams[k]: v for k, v in DEFENCE.items()}),
        intercept=INTERCEPT,
        home_advantage=HOME_ADVANTAGE,
        rho=RHO,
        xi=0.0019,
        as_of=date(2026, 10, 22),
        n_matches=200,
        log_likelihood=0.0,
        iterations=1,
        converged=True,
        termination="hand made",
        projected_gradient=0.0,
    )


class HandModels:
    """Stands in for the model cache: always the hand-made league (records the cut-off days)."""

    def __init__(self) -> None:
        self.calls: list[date] = []
        counts = {team_key(FIXTURE.home): 30, team_key(FIXTURE.away): 30}
        self.fitted = FittedLeague(hand_model(), MappingProxyType(counts))

    async def get(self, _league: object, as_of: date) -> FittedLeague:
        self.calls.append(as_of)
        return self.fitted


def batch(prices: Prices, taken: datetime = MORNING) -> HistoryBatch:
    rows = [
        *snapshot(FIXTURE, BOOKMAKER_PINNACLE, ONE_X_TWO, PINNACLE, kind=OddsKind.MORNING,
                  taken_at=taken),
        *snapshot(FIXTURE, BOOKMAKER_1XBET, ONE_X_TWO, x12(prices.home, prices.draw, prices.away),
                  kind=OddsKind.MORNING, taken_at=taken),
        *snapshot(FIXTURE, BOOKMAKER_1XBET, HANDICAP,
                  {Selection.HOME: prices.ah_home, Selection.AWAY: prices.ah_away},
                  kind=OddsKind.MORNING, taken_at=taken),
    ]  # fmt: skip
    return HistoryBatch(fixtures=(FIXTURE,), odds=tuple(rows))


async def analyse(bare: Bare) -> dict[tuple[Market, Selection], SignalView]:
    """A custom analysis of today: every selection of both markets comes back."""
    services = bare.system.services
    settings = await services.settings.get_settings(USER)
    await services.settings.update_settings(
        USER,
        replace(
            settings,
            min_probability=MIN_PROBABILITY,
            min_ev=0.0,
            odds_min=1.01,
            odds_max=20.0,
            markets=frozenset({MarketType.MATCH_RESULT, MarketType.ASIAN_HANDICAP}),
            quiet_hours=None,
            bank=BANK,
            unit=UNIT,
        ),
    )
    query = replace(
        AnalysisQuery.from_settings(await services.settings.get_settings(USER), DateSpec.today()),
        include_no_value=True,
    )
    result = await services.analysis.analyze(USER, query)
    return {(v.market, v.selection): v for v in result.signals}


@pytest.fixture(params=[WORLD_A, WORLD_B], ids=["cap-binds", "cap-free"])
async def scenario(request: pytest.FixtureRequest, tmp_path: Path):  # type: ignore[no-untyped-def]
    prices: Prices = request.param
    config = PipelineConfig(staking=StakingMode.KELLY, model_weight=MODEL_WEIGHT)
    async with open_bare(tmp_path / "hand.db", batch(prices), config=config) as bare:
        models = HandModels()
        object.__setattr__(bare.ctx, "models", models)
        yield prices, await analyse(bare), models


# ------------------------------------------------------------------------------ tests
def test_the_oracle_itself_is_a_distribution() -> None:
    assert grid().sum() == pytest.approx(1.0, abs=1e-15)
    assert sum(model_1x2().values()) == pytest.approx(1.0, abs=1e-15)
    for home_side in (True, False):
        assert sum(asian_distribution(-0.25, home_side=home_side).values()) == pytest.approx(1.0)
    # hand-checked once: lambda = e^0.55, mu = e^-0.15 -> 57.4% / 24.9% / 17.8%
    assert model_1x2() == pytest.approx(
        {Selection.HOME: 0.57376, Selection.DRAW: 0.24872, Selection.AWAY: 0.17751}, abs=5e-6
    )


@pytest.mark.parametrize("selection", [Selection.HOME, Selection.DRAW, Selection.AWAY])
def test_1x2_probability_fair_price_and_ev(
    scenario,
    selection: Selection,  # type: ignore[no-untyped-def]
) -> None:
    prices, views, _ = scenario
    view = views[ONE_X_TWO, selection]
    probability = ensemble_1x2()[selection]
    odds = {Selection.HOME: prices.home, Selection.DRAW: prices.draw, Selection.AWAY: prices.away}[
        selection
    ]
    assert view.odds == odds
    assert view.bookmaker == BOOKMAKER_1XBET
    assert view.probability == pytest.approx(probability, abs=1e-9)
    assert view.fair_odds == pytest.approx(1 / probability, abs=1e-8)
    assert view.ev == pytest.approx(probability * odds - 1, abs=1e-9)


def test_1x2_stakes_are_quarter_kelly_capped_at_two_percent(scenario) -> None:  # type: ignore[no-untyped-def]
    prices, views, _ = scenario
    probability = ensemble_1x2()
    for selection, odds in (
        (Selection.HOME, prices.home),
        (Selection.DRAW, prices.draw),
        (Selection.AWAY, prices.away),
    ):
        ev = probability[selection] * odds - 1
        recommended = probability[selection] >= MIN_PROBABILITY and ev >= 0
        expected = kelly_units(ev / (odds - 1)) if recommended else Decimal(0)
        view = views[ONE_X_TWO, selection]
        assert view.stake_units == expected
        assert view.no_value is not recommended


def test_the_stake_cap_binds_in_one_world_and_not_in_the_other(scenario) -> None:  # type: ignore[no-untyped-def]
    prices, views, _ = scenario
    capped = prices == WORLD_A
    assert views[ONE_X_TWO, Selection.HOME].stake_units == (
        Decimal("2.0") if capped else Decimal("1.2")
    )
    # the draw (24.9%) is below the 30% minimum probability: shown, never staked
    assert views[ONE_X_TWO, Selection.DRAW].stake_units == Decimal(0)
    assert views[HANDICAP, Selection.HOME].stake_units == (
        Decimal("2.0") if capped else Decimal("1.7")
    )
    assert views[ONE_X_TWO, Selection.AWAY].stake_units == Decimal(0)
    assert views[HANDICAP, Selection.AWAY].stake_units == Decimal(0)


@pytest.mark.parametrize("home_side", [True, False], ids=["home", "away"])
def test_quarter_line_asian_pick_probability_ev_and_stake(scenario, home_side: bool) -> None:  # type: ignore[no-untyped-def]
    prices, views, _ = scenario
    distribution = asian_distribution(-0.25, home_side=home_side)
    selection = Selection.HOME if home_side else Selection.AWAY
    odds = prices.ah_home if home_side else prices.ah_away
    view = views[HANDICAP, selection]

    assert view.odds == odds
    # the displayed probability is the chance of ending in profit (win + half win)
    assert view.probability == pytest.approx(
        distribution["win"] + distribution["half_win"], abs=1e-9
    )
    assert view.ev == pytest.approx(asian_ev(distribution, odds), abs=1e-9)
    ev = asian_ev(distribution, odds)
    expected = kelly_units(full_kelly(distribution, odds)) if ev > 0 else Decimal(0)
    assert view.stake_units == expected


def test_the_asian_pick_has_no_market_anchor_and_says_so(scenario) -> None:  # type: ignore[no-untyped-def]
    _, views, _ = scenario
    assert "no_market_reference" in views[HANDICAP, Selection.HOME].risk_flags
    assert "no_market_reference" not in views[ONE_X_TWO, Selection.HOME].risk_flags


def test_the_model_is_asked_for_matches_before_tomorrow_only(scenario) -> None:  # type: ignore[no-untyped-def]
    _, _, models = scenario
    assert models.calls
    assert set(models.calls) == {date(2026, 10, 22)}  # as_of is exclusive: strictly before it


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA-T4-F5: SignalView.fair_odds is 1/probability; for a quarter-line handicap the "
        "probability counts half wins, so it is not the zero-EV price the contract promises"
    ),
)
def test_fair_price_of_a_quarter_line_is_the_zero_ev_price(scenario) -> None:  # type: ignore[no-untyped-def]
    _, views, _ = scenario
    view = views[HANDICAP, Selection.HOME]
    expected = asian_fair_odds(asian_distribution(-0.25, home_side=True))
    assert view.fair_odds == pytest.approx(expected, abs=1e-6)


async def test_flat_staking_is_one_unit_on_positive_ev_only(tmp_path: Path) -> None:
    config = PipelineConfig(staking=StakingMode.FLAT, model_weight=MODEL_WEIGHT)
    async with open_bare(tmp_path / "flat.db", batch(WORLD_A), config=config) as bare:
        object.__setattr__(bare.ctx, "models", HandModels())
        views = await analyse(bare)

    assert views[ONE_X_TWO, Selection.HOME].stake_units == Decimal(1)
    assert views[ONE_X_TWO, Selection.DRAW].stake_units == Decimal(0)  # below 30% probability
    assert views[HANDICAP, Selection.HOME].stake_units == Decimal(1)
    assert views[ONE_X_TWO, Selection.AWAY].stake_units == Decimal(0)  # EV < 0: never staked
    assert views[HANDICAP, Selection.AWAY].stake_units == Decimal(0)


def test_a_small_edge_gives_uncapped_quarter_kelly_by_hand() -> None:
    """Draw at 4.40: p = 0.24936, EV = 9.72%, full Kelly 2.859%, a quarter 0.7146% of 1000."""
    taken = utc(2026, 10, 21, 5)
    quotes = [
        MarketQuote(
            fixture=FIXTURE.ref,
            bookmaker=bookmaker,
            kind=OddsKind.RECALC,
            market=ONE_X_TWO,
            prices=prices,
            taken_at=taken,
        )
        for bookmaker, prices in (
            (BOOKMAKER_PINNACLE, PINNACLE),
            (BOOKMAKER_1XBET, x12(2.10, 4.40, 4.00)),
        )
    ]
    config = PipelineConfig(staking=StakingMode.KELLY, model_weight=MODEL_WEIGHT)
    valuation = value_fixture(
        ValuationInput(
            fixture=FIXTURE,
            quotes=quotes,
            fitted=HandModels().fitted,
            now=utc(2026, 10, 21, 6),
            markets=frozenset({MarketType.MATCH_RESULT}),
        ),
        config,
    )
    draw = next(c for c in valuation.candidates if c.selection is Selection.DRAW)

    assert draw.probability == pytest.approx(0.249362, abs=1e-6)
    assert draw.ev == pytest.approx(0.097192, abs=1e-6)
    # 1000 * 0.007146... = 7.14 (floored to cents) -> 0.714 units -> 0.7 (floored to tenths)
    assert stake_units(draw, config, bank=BANK, unit=UNIT) == Decimal("0.7")
