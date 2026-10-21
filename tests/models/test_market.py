import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain import (
    BOOKMAKER_BET365,
    BOOKMAKER_MARKET_AVERAGE,
    BOOKMAKER_MARKET_MAXIMUM,
    Market,
    OddsKind,
    Selection,
)
from app.models import (
    DevigMethod,
    MarketModelConfig,
    MarketQuote,
    ModelInputError,
    asian_fair_odds,
    asian_from_two_way,
    complete_quotes,
    devig,
    devig_quote,
    double_chance_from_match_result,
    fair_markets,
    market_from_matrix,
    score_matrix,
)
from app.models.asian import AsianOutcomeProbabilities
from tests.models.domain_helpers import fixture, snapshots

MATCH = fixture()
ONE_X_TWO = {Selection.HOME: 2.05, Selection.DRAW: 3.5, Selection.AWAY: 3.8}
MATRIX = score_matrix(1.5, 1.1, -0.05)

probabilities = st.floats(min_value=0.0, max_value=1.0)


def quote(market: Market, prices: dict[Selection, float]) -> MarketQuote:
    (single,) = complete_quotes(snapshots(MATCH, market, prices))
    return single


def test_partition_market_is_plain_devig() -> None:
    fair = devig_quote(quote(Market.match_result(), ONE_X_TWO))
    expected = devig(tuple(ONE_X_TWO.values()), DevigMethod.SHIN)
    assert tuple(
        fair.win_probability(s) for s in Market.match_result().selections
    ) == pytest.approx(expected)
    proportional = devig_quote(
        quote(Market.match_result(), ONE_X_TWO), method=DevigMethod.PROPORTIONAL
    )
    assert proportional.win_probability(Selection.HOME) == pytest.approx(
        (1 / 2.05) / sum(1 / o for o in ONE_X_TWO.values())
    )


def test_double_chance_quote_is_scaled_to_two() -> None:
    prices = {
        Selection.HOME_OR_DRAW: 1.25,
        Selection.HOME_OR_AWAY: 1.3,
        Selection.DRAW_OR_AWAY: 1.8,
    }
    fair = devig_quote(quote(Market.double_chance(), prices))
    total = math.fsum(fair.win_probability(s) for s in prices)
    assert total == pytest.approx(2.0)


def test_double_chance_from_match_result() -> None:
    result = market_from_matrix(MATRIX, Market.match_result())
    chance = double_chance_from_match_result(result)
    assert chance == market_from_matrix(MATRIX, Market.double_chance())
    with pytest.raises(ModelInputError, match="1X2"):
        double_chance_from_match_result(market_from_matrix(MATRIX, Market.btts()))


@pytest.mark.parametrize("line", [2.0, 2.25, 2.75, 3.0])
def test_asian_two_way_recovers_the_truth_from_fair_prices(line: float) -> None:
    # Fair two-way prices of a known distribution: 1/o_over + 1/o_under = 1 exactly.
    truth = market_from_matrix(MATRIX, Market.totals(line))
    over = truth.outcomes[Selection.OVER]
    p_over = 1.0 / asian_fair_odds(over)
    p_under = 1.0 / asian_fair_odds(truth.outcomes[Selection.UNDER])
    assert p_over + p_under == pytest.approx(1.0)
    recovered = asian_from_two_way(p_over, reference=over)
    assert recovered.as_tuple() == pytest.approx(over.as_tuple(), abs=1e-12)


@given(probabilities, st.sampled_from([-1.25, -1.0, -0.75, 0.0, 0.25, 1.5]))
def test_asian_two_way_is_a_distribution_that_prices_fairly(p_first: float, line: float) -> None:
    reference = market_from_matrix(MATRIX, Market.asian_handicap(line)).outcomes[Selection.HOME]
    outcomes = asian_from_two_way(p_first, reference)
    assert math.fsum(outcomes.as_tuple()) == pytest.approx(1.0)
    assert min(outcomes.as_tuple()) >= 0.0
    gain = outcomes.win + outcomes.half_win / 2
    if 1e-9 < p_first < 1 - 1e-9 and gain > 0:
        # The first side's fair odds equal the de-vigged price whenever no clamping happened.
        middle_kept = math.isclose(outcomes.push, reference.push, rel_tol=1e-9, abs_tol=1e-15)
        if middle_kept:
            assert 1.0 / asian_fair_odds(outcomes) == pytest.approx(p_first, abs=1e-9)


def test_asian_two_way_scales_middle_mass_for_extreme_prices() -> None:
    reference = AsianOutcomeProbabilities(0.2, 0.0, 0.0, 0.5, 0.3)  # e.g. over 2.25
    extreme = asian_from_two_way(0.99, reference)  # under side would need negative mass
    assert extreme.half_loss < reference.half_loss
    assert min(extreme.as_tuple()) >= 0.0
    with pytest.raises(ModelInputError):
        asian_from_two_way(1.5, reference)


def test_whole_line_needs_a_matching_reference() -> None:
    whole = quote(Market.totals(3.0), {Selection.OVER: 2.4, Selection.UNDER: 1.6})
    with pytest.raises(ModelInputError, match="reference"):
        devig_quote(whole)
    with pytest.raises(ModelInputError, match="reference"):
        devig_quote(whole, reference=market_from_matrix(MATRIX, Market.totals(2.0)))
    reference = market_from_matrix(MATRIX, Market.totals(3.0))
    fair = devig_quote(whole, reference=reference)
    assert fair.outcomes[Selection.OVER].push == pytest.approx(
        reference.outcomes[Selection.OVER].push
    )


def all_quotes() -> list[MarketQuote]:
    rows = [
        *snapshots(MATCH, Market.match_result(), ONE_X_TWO),
        *snapshots(MATCH, Market.match_result(), ONE_X_TWO, bookmaker=BOOKMAKER_MARKET_MAXIMUM),
        *snapshots(
            MATCH,
            Market.totals(2.5),
            {Selection.OVER: 1.95, Selection.UNDER: 1.95},
            bookmaker=BOOKMAKER_MARKET_AVERAGE,
        ),
        *snapshots(
            MATCH,
            Market.btts(),
            {Selection.YES: 1.8, Selection.NO: 2.0},
            bookmaker=BOOKMAKER_BET365,
        ),
        *snapshots(MATCH, Market.asian_handicap(-0.25), {Selection.HOME: 1.9, Selection.AWAY: 2.0}),
        *snapshots(MATCH, Market.match_result(), ONE_X_TWO, kind=OddsKind.CLOSING),
    ]
    return complete_quotes(rows)


def test_fair_markets_priority_fallback_and_derived_double_chance() -> None:
    fair = fair_markets(all_quotes(), kind=OddsKind.OPENING)
    assert set(fair) == {Market.match_result(), Market.totals(2.5), Market.double_chance()}
    assert fair[Market.match_result()].bookmaker == "pinnacle"
    assert not fair[Market.match_result()].wide_uncertainty
    assert fair[Market.match_result()].overround == pytest.approx(
        sum(1 / o for o in ONE_X_TWO.values()) - 1
    )
    assert fair[Market.totals(2.5)].wide_uncertainty  # only the market average priced it
    derived = fair[Market.double_chance()]
    assert derived.derived
    assert derived.market == Market.double_chance()
    # BTTS only at bet365 (not trusted); quarter handicap has no reference: both skipped.


def test_fair_markets_uses_references_for_quarter_lines_and_filters_kind() -> None:
    reference = market_from_matrix(MATRIX, Market.asian_handicap(-0.25))
    fair = fair_markets(
        all_quotes(), kind=OddsKind.OPENING, references={Market.asian_handicap(-0.25): reference}
    )
    assert Market.asian_handicap(-0.25) in fair
    closing = fair_markets(all_quotes(), kind=OddsKind.CLOSING)
    assert set(closing) == {Market.match_result(), Market.double_chance()}
    assert closing[Market.match_result()].kind is OddsKind.CLOSING


def test_fair_markets_keeps_an_own_double_chance_quote() -> None:
    rows = [
        *snapshots(MATCH, Market.match_result(), ONE_X_TWO),
        *snapshots(
            MATCH,
            Market.double_chance(),
            {
                Selection.HOME_OR_DRAW: 1.25,
                Selection.HOME_OR_AWAY: 1.3,
                Selection.DRAW_OR_AWAY: 1.8,
            },
        ),
    ]
    fair = fair_markets(complete_quotes(rows), kind=OddsKind.OPENING)
    assert not fair[Market.double_chance()].derived


def test_fair_markets_rejects_quotes_of_several_fixtures() -> None:
    other = fixture("X", "Y")
    rows = [
        *snapshots(MATCH, Market.match_result(), ONE_X_TWO),
        *snapshots(other, Market.match_result(), ONE_X_TWO),
    ]
    with pytest.raises(ModelInputError, match="one fixture"):
        fair_markets(complete_quotes(rows), kind=OddsKind.OPENING)


def test_market_model_config_validation() -> None:
    with pytest.raises(ModelInputError, match="at least one"):
        MarketModelConfig(priority=())
    with pytest.raises(ModelInputError, match="both"):
        MarketModelConfig(priority=("pinnacle",), fallback=("pinnacle",))
    custom = MarketModelConfig(priority=(BOOKMAKER_MARKET_AVERAGE,), fallback=())
    fair = fair_markets(all_quotes(), kind=OddsKind.OPENING, config=custom)
    assert set(fair) == {Market.totals(2.5)}
    assert not fair[Market.totals(2.5)].wide_uncertainty
