import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain import Market, OddsKind, Selection
from app.models import (
    FairMarket,
    ModelInputError,
    asian_expected_value,
    ensemble,
    ensemble_markets,
    is_partition_market,
    market_from_matrix,
    mix_outcomes,
    score_matrix,
)

MODEL = score_matrix(1.7, 0.9, -0.1, max_goals=12)
MARKET = score_matrix(1.4, 1.1, -0.05, max_goals=12)
MARKETS = (
    Market.match_result(),
    Market.double_chance(),
    Market.btts(),
    Market.totals(2.5),
    Market.totals(2.25),
    Market.asian_handicap(-0.75),
    Market.asian_handicap(-1.0),
)
weights = st.floats(min_value=0.0, max_value=1.0)


@given(weights, st.sampled_from(MARKETS))
def test_ensemble_is_the_per_selection_mix(weight: float, market: Market) -> None:
    model = market_from_matrix(MODEL, market)
    fair = market_from_matrix(MARKET, market)
    mixed = ensemble(model, fair, weight)
    for selection in market.selections:
        expected = [
            weight * a + (1 - weight) * b
            for a, b in zip(
                model.outcomes[selection].as_tuple(),
                fair.outcomes[selection].as_tuple(),
                strict=True,
            )
        ]
        assert mixed.outcomes[selection].as_tuple() == pytest.approx(expected, abs=1e-12)
    if is_partition_market(market):
        total = math.fsum(mixed.win_probability(s) for s in market.selections)
        assert total == pytest.approx(1.0, abs=1e-12)


@given(weights)
def test_mixing_score_matrices_matches_mixing_quarter_line_distributions(weight: float) -> None:
    # Consistency: mixing per selection equals the market of the mixed score distribution.
    market = Market.asian_handicap(-0.75)
    mixed = ensemble(market_from_matrix(MODEL, market), market_from_matrix(MARKET, market), weight)
    pooled = market_from_matrix(weight * MODEL + (1 - weight) * MARKET, market)
    for selection in market.selections:
        assert mixed.outcomes[selection].as_tuple() == pytest.approx(
            pooled.outcomes[selection].as_tuple(), abs=1e-9
        )
    # And the second side stays the mirror of the first.
    assert mixed.outcomes[Selection.AWAY] == mixed.outcomes[Selection.HOME].opposite()


def test_ensemble_extremes_return_each_input() -> None:
    model = market_from_matrix(MODEL, Market.match_result())
    fair = market_from_matrix(MARKET, Market.match_result())
    for weight, expected in ((1.0, model), (0.0, fair)):
        mixed = ensemble(model, fair, weight)
        for selection in Market.match_result().selections:
            assert mixed.win_probability(selection) == pytest.approx(
                expected.win_probability(selection)
            )


def test_ensemble_validation() -> None:
    model = market_from_matrix(MODEL, Market.match_result())
    with pytest.raises(ModelInputError, match="cannot mix"):
        ensemble(model, market_from_matrix(MARKET, Market.btts()), 0.5)
    with pytest.raises(ModelInputError, match="weight"):
        ensemble(model, model, 1.5)
    outcome = model.outcomes[Selection.HOME]
    with pytest.raises(ModelInputError, match="weight"):
        mix_outcomes(outcome, outcome, math.nan)


def test_ensemble_markets_skips_markets_without_a_market_estimate() -> None:
    model = {m: market_from_matrix(MODEL, m) for m in (Market.match_result(), Market.btts())}
    fair = {
        Market.match_result(): FairMarket(
            probabilities=market_from_matrix(MARKET, Market.match_result()),
            bookmaker="pinnacle",
            kind=OddsKind.OPENING,
            overround=0.03,
            wide_uncertainty=False,
        )
    }
    mixed = ensemble_markets(model, fair, 0.5)
    assert list(mixed) == [Market.match_result()]
    # EV of the mix lies between the two inputs' EVs (EV is linear in the distribution).
    odds = 2.0
    evs = sorted(
        asian_expected_value(p.outcomes[Selection.HOME], odds)
        for p in (model[Market.match_result()], fair[Market.match_result()].probabilities)
    )
    ev = asian_expected_value(mixed[Market.match_result()].outcomes[Selection.HOME], odds)
    assert evs[0] <= ev <= evs[1]
