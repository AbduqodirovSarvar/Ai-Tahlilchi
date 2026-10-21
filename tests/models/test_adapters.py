from datetime import UTC, date, datetime, timedelta

import pytest

from app.domain import (
    BOOKMAKER_MARKET_AVERAGE,
    ExternalRef,
    FixtureResult,
    Market,
    OddsKind,
    ResultStatus,
    Selection,
)
from app.models import (
    MarketQuote,
    ModelInputError,
    complete_quotes,
    match_date,
    match_record,
    match_records,
    quotes_by_fixture,
    team_key,
)
from tests.models.domain_helpers import finished, fixture, snapshots, team

ONE_X_TWO = {Selection.HOME: 2.1, Selection.DRAW: 3.4, Selection.AWAY: 3.6}


def test_team_key_is_the_external_reference() -> None:
    assert team_key(team("Nott'm Forest")) == "test:Nott'm Forest"


def test_match_date_is_the_utc_calendar_date_of_kickoff() -> None:
    # 23:30 in New York on 1 March is 04:30 UTC on 2 March.
    late = fixture(kickoff=datetime(2025, 3, 2, 4, 30, tzinfo=UTC))
    assert match_date(late) == date(2025, 3, 2)


def test_match_record_from_finished_result() -> None:
    match = fixture("A", "B")
    record = match_record(match, finished(match, 2, 1))
    assert record is not None
    assert (record.home_team, record.away_team) == ("test:A", "test:B")
    assert (record.home_goals, record.away_goals, record.match_date) == (2, 1, date(2025, 3, 1))


@pytest.mark.parametrize(
    "result",
    [
        FixtureResult(fixture=ExternalRef("test", "x"), status=ResultStatus.POSTPONED),
        FixtureResult(
            fixture=ExternalRef("test", "x"),
            status=ResultStatus.ABANDONED,
            home_score=1,
            away_score=0,
        ),
    ],
)
def test_unfinished_results_give_no_record(result: FixtureResult) -> None:
    match = fixture(ident="x")
    assert match_record(match, result) is None


def test_result_of_another_fixture_is_rejected() -> None:
    with pytest.raises(ModelInputError, match="does not belong"):
        match_record(fixture("A", "B"), finished(fixture("C", "D"), 1, 1))


def test_match_records_joins_and_skips_fixtures_without_result() -> None:
    first, second = fixture("A", "B"), fixture("C", "D")
    records = match_records([first, second], [finished(second, 0, 0)])
    assert [r.home_team for r in records] == ["test:C"]


def test_complete_quotes_keeps_only_complete_sets_and_latest_price() -> None:
    match = fixture()
    early = match.kickoff - timedelta(days=2)
    rows = [
        *snapshots(match, Market.match_result(), ONE_X_TWO, taken_at=early),
        *snapshots(match, Market.match_result(), {Selection.HOME: 2.0}),  # newer home price
        # Incomplete: the under is missing.
        *snapshots(match, Market.totals(2.5), {Selection.OVER: 1.9}),
        *snapshots(match, Market.match_result(), ONE_X_TWO, bookmaker=BOOKMAKER_MARKET_AVERAGE),
    ]
    quotes = complete_quotes(rows)
    assert [(q.bookmaker, q.market) for q in quotes] == [
        ("pinnacle", Market.match_result()),
        ("market_avg", Market.match_result()),
    ]
    pinnacle = quotes[0]
    assert pinnacle.prices[Selection.HOME] == 2.0
    assert pinnacle.taken_at == match.kickoff - timedelta(days=1)
    assert pinnacle.ordered_prices() == (2.0, 3.4, 3.6)
    assert list(quotes_by_fixture(quotes)) == [match.ref]


def test_market_quote_validates_its_prices() -> None:
    common = {
        "fixture": ExternalRef("test", "x"),
        "bookmaker": "pinnacle",
        "kind": OddsKind.OPENING,
        "market": Market.btts(),
        "taken_at": datetime(2025, 1, 1, tzinfo=UTC),
    }
    with pytest.raises(ModelInputError, match="must price"):
        MarketQuote(prices={Selection.YES: 1.9}, **common)
    with pytest.raises(ModelInputError, match="odds <= 1"):
        MarketQuote(prices={Selection.YES: 1.0, Selection.NO: 1.5}, **common)
