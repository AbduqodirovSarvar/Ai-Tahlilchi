from dataclasses import replace

import pytest

from app.models import ModelInputError
from app.models.asian import settlement_returns
from app.models.backtest import (
    Bet,
    BetFilter,
    PricingConfig,
    StakingConfig,
    WalkForwardResult,
    candidate_bets,
    select_bets,
)
from app.models.staking import DEFAULT_MAX_STAKE_FRACTION


@pytest.fixture(scope="module")
def candidates(small_run: WalkForwardResult) -> list[Bet]:
    return candidate_bets(
        small_run.predictions, weight=0.5, pricing=PricingConfig(), staking=StakingConfig()
    )


def test_candidates_are_settled_consistently(candidates: list[Bet]) -> None:
    assert candidates
    for bet in candidates:
        unit = settlement_returns(bet.odds)[bet.settlement]
        assert bet.flat_profit == pytest.approx(unit)
        assert bet.kelly_profit == pytest.approx(bet.kelly_stake * unit)
        assert 0.0 <= bet.kelly_stake <= DEFAULT_MAX_STAKE_FRACTION * 100.0 + 1e-12
        if bet.ev <= 0:
            assert bet.kelly_stake == 0.0
        assert bet.closing_odds is not None
        assert bet.closing_profit == pytest.approx(
            settlement_returns(bet.closing_odds)[bet.settlement]
        )
        assert bet.clv is not None
        assert 0.0 <= bet.probability <= 1.0


def test_select_bets_keeps_the_best_passing_candidate_per_fixture(
    candidates: list[Bet],
) -> None:
    open_filter = BetFilter(min_probability=0.0, min_ev=-1.0, odds_min=1.01, odds_max=1000.0)
    chosen = select_bets(candidates, open_filter)
    assert len({b.fixture for b in chosen}) == len(chosen) == len({c.fixture for c in candidates})
    for bet in chosen:
        best = max(c.ev for c in candidates if c.fixture == bet.fixture)
        assert bet.ev == best
    strict = BetFilter(min_probability=0.5, min_ev=0.02, odds_min=1.5, odds_max=2.5)
    for bet in select_bets(candidates, strict):
        assert bet.probability >= 0.5
        assert bet.ev >= 0.02
        assert 1.5 <= bet.odds <= 2.5
    dates = [b.match_date for b in chosen]
    assert dates == sorted(dates)


def test_missing_bet_price_bookmaker_gives_no_candidates(small_run: WalkForwardResult) -> None:
    pricing = PricingConfig(bookmaker="nobody")
    assert (
        candidate_bets(small_run.predictions, weight=0.5, pricing=pricing, staking=StakingConfig())
        == []
    )


def test_without_closing_data_closing_fields_are_none(small_run: WalkForwardResult) -> None:
    prediction = replace(small_run.predictions[0], closing={})
    item = replace(
        prediction.item,
        quotes=tuple(q for q in prediction.item.quotes if q.kind.value != "closing"),
    )
    prediction = replace(prediction, item=item)
    bets = candidate_bets(
        [prediction], weight=0.5, pricing=PricingConfig(), staking=StakingConfig()
    )
    assert bets
    for bet in bets:
        assert bet.closing_fair_odds is None
        assert bet.closing_ev is None
        assert bet.closing_odds is None
        assert bet.closing_profit is None
        assert bet.clv is None


@pytest.mark.parametrize(
    "change",
    [{"odds_min": 1.0}, {"odds_min": 3.0, "odds_max": 2.0}, {"min_probability": 1.5}],
)
def test_bet_filter_validation(change: dict[str, float]) -> None:
    with pytest.raises(ModelInputError):
        BetFilter(**change)
