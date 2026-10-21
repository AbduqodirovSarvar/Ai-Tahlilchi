"""API-Football → domain mapping: statuses, scores, odds markets, context data."""

from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.clients.api_football import validate_records
from app.clients.api_football_models import (
    Envelope,
    FixtureItem,
    InjuryItem,
    LineupItem,
    OddsItem,
    ScorePair,
    TeamStatisticsItem,
)
from app.domain import (
    DomainValidationError,
    ExternalRef,
    FixtureStatus,
    Market,
    OddsKind,
    ResultStatus,
    Selection,
)
from app.sports.football_mapping import (
    HandicapConvention,
    bookmaker_slug,
    expected_goals,
    fixture_status,
    map_fixture,
    map_injuries,
    map_lineups,
    map_odds,
    map_result,
)
from tests.clients.conftest import load

TAKEN_AT = datetime(2024, 10, 26, 8, 0, tzinfo=UTC)


def records[M: (FixtureItem, OddsItem, InjuryItem, LineupItem, TeamStatisticsItem)](
    name: str, model: type[M]
) -> list[M]:
    return validate_records(Envelope.model_validate(load(name)), model, endpoint=name)


def by_id(name: str) -> dict[int, FixtureItem]:
    return {item.fixture.id: item for item in records(name, FixtureItem)}


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("NS", FixtureStatus.SCHEDULED),
        ("tbd", FixtureStatus.SCHEDULED),
        ("1H", FixtureStatus.LIVE),
        ("SUSP", FixtureStatus.LIVE),
        ("FT", FixtureStatus.FINISHED),
        ("AET", FixtureStatus.FINISHED),
        ("PEN", FixtureStatus.FINISHED),
        ("PST", FixtureStatus.POSTPONED),
        ("CANC", FixtureStatus.CANCELLED),
        ("ABD", FixtureStatus.ABANDONED),
        ("AWD", FixtureStatus.ABANDONED),
        ("WO", FixtureStatus.ABANDONED),
    ],
)
def test_status_codes(code: str, status: FixtureStatus) -> None:
    assert fixture_status(code) is status


def test_unknown_status_code_is_rejected() -> None:
    with pytest.raises(DomainValidationError, match="XYZ"):
        fixture_status("XYZ")


def test_fixture_mapping_uses_api_football_refs_and_utc() -> None:
    fixture = map_fixture(by_id("fixtures_league_39_2024.json")[1208028])
    assert fixture.ref == ExternalRef("api-football", "1208028")
    assert fixture.league.ref == ExternalRef("api-football", "39")
    assert fixture.season == 2024
    assert fixture.kickoff == datetime(2024, 8, 18, 1, 30, tzinfo=UTC)
    assert (fixture.home.name, fixture.away.name) == ("Liverpool", "Ipswich")
    assert fixture.status is FixtureStatus.FINISHED


def test_finished_result_with_half_time() -> None:
    result = map_result(by_id("fixtures_league_39_2024.json")[1208023])
    assert result is not None
    assert result.status is ResultStatus.FINISHED
    assert (result.home_score, result.away_score) == (2, 0)
    assert (result.home_ht_score, result.away_ht_score) == (1, 0)


def test_extra_time_result_uses_regular_time_score() -> None:
    items = by_id("fixtures_date_2024-10-26.json")
    aet = map_result(items[1208102])
    assert aet is not None
    assert (aet.home_score, aet.away_score) == (1, 1)  # goals were 2-1 after extra time
    pen = map_result(items[1208103])
    assert pen is not None
    assert (pen.home_score, pen.away_score) == (0, 0)


def test_extra_time_without_fulltime_score_is_invalid() -> None:
    item = by_id("fixtures_date_2024-10-26.json")[1208102]
    no_fulltime = item.score.model_copy(update={"fulltime": ScorePair()})
    broken = item.model_copy(update={"score": no_fulltime})
    with pytest.raises(DomainValidationError):
        map_result(broken)


def test_unplayed_and_awarded_results_have_no_score() -> None:
    league = by_id("fixtures_league_39_2024.json")
    day = by_id("fixtures_date_2024-10-26.json")
    for item, status in (
        (league[1208024], ResultStatus.POSTPONED),
        (league[1208025], ResultStatus.CANCELLED),
        (day[1208106], ResultStatus.ABANDONED),
    ):
        result = map_result(item)
        assert result is not None
        assert result.status is status
        assert result.home_score is None


def test_abandoned_keeps_score_at_stoppage() -> None:
    result = map_result(by_id("fixtures_league_39_2024.json")[1208026])
    assert result is not None
    assert (result.status, result.home_score) == (ResultStatus.ABANDONED, 1)


def test_scheduled_and_live_have_no_result() -> None:
    day = by_id("fixtures_date_2024-10-26.json")
    assert map_result(day[1208100]) is None
    assert map_result(day[1208101]) is None


def test_inconsistent_half_time_is_dropped() -> None:
    item = by_id("fixtures_league_39_2024.json")[1208021]
    bad_ht = item.score.model_copy(
        update={"halftime": item.score.fulltime.model_copy(update={"home": 5})}
    )
    result = map_result(item.model_copy(update={"score": bad_ht}))
    assert result is not None
    assert result.home_ht_score is None
    assert result.home_score == 1


def test_plain_ft_falls_back_to_goals() -> None:
    item = by_id("fixtures_league_39_2024.json")[1208021]
    empty = item.score.model_copy(update={"fulltime": item.score.extratime})
    result = map_result(item.model_copy(update={"score": empty}))
    assert result is not None
    assert (result.home_score, result.away_score) == (1, 0)


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("1xBet", "1xbet"),
        ("Pinnacle", "pinnacle"),
        ("Bet365", "bet365"),
        ("William Hill", "william-hill"),
        (" 10Bet ", "10bet"),
    ],
)
def test_bookmaker_slugs(name: str, slug: str) -> None:
    assert bookmaker_slug(name) == slug


def odds_snapshots(convention: HandicapConvention = HandicapConvention.HOME_LINE):
    items = records("odds_fixture_1208100.json", OddsItem)
    return map_odds(items, kind=OddsKind.MORNING, taken_at=TAKEN_AT, convention=convention)


def price(snapshots, bookmaker: str, market: Market, selection: Selection) -> float:
    found = [
        s.odds
        for s in snapshots
        if (s.bookmaker, s.market, s.selection) == (bookmaker, market, selection)
    ]
    assert len(found) == 1, found
    return found[0]


def test_odds_markets_are_mapped() -> None:
    snapshots, stats = odds_snapshots()
    assert price(snapshots, "pinnacle", Market.match_result(), Selection.HOME) == 1.85
    assert price(snapshots, "pinnacle", Market.double_chance(), Selection.DRAW_OR_AWAY) == 1.95
    assert price(snapshots, "pinnacle", Market.totals(2.5), Selection.UNDER) == 1.95
    assert price(snapshots, "bet365", Market.totals(2.75), Selection.OVER) == 2.10
    assert price(snapshots, "pinnacle", Market.btts(), Selection.YES) == 1.80
    assert price(snapshots, "1xbet", Market.match_result(), Selection.HOME) == 1.88
    assert {s.kind for s in snapshots} == {OddsKind.MORNING}
    assert {s.taken_at for s in snapshots} == {TAKEN_AT}
    assert stats.unknown_bets == {"Exact Score", "Goals Over/Under First Half"}
    # Over 2.3 (off grid) x2, 1xBet draw at 1.00, "not-a-number", Unibet "Maybe".
    assert stats.invalid_prices == 5


def test_no_totals_off_grid_and_no_duplicate_selection() -> None:
    snapshots, _ = odds_snapshots()
    lines = {s.market.line for s in snapshots if s.market.type.value == "totals"}
    assert lines == {1.5, 2.5, 2.75}
    keys = [(s.bookmaker, s.market, s.selection) for s in snapshots]
    assert len(keys) == len(set(keys))


def test_asian_handicap_home_line_convention() -> None:
    snapshots, _ = odds_snapshots()
    assert price(snapshots, "pinnacle", Market.asian_handicap(-0.5), Selection.HOME) == 1.95
    assert price(snapshots, "pinnacle", Market.asian_handicap(-0.5), Selection.AWAY) == 1.90
    assert price(snapshots, "pinnacle", Market.asian_handicap(0), Selection.AWAY) == 2.45


def test_asian_handicap_own_line_convention() -> None:
    snapshots, _ = odds_snapshots(HandicapConvention.OWN_LINE)
    assert price(snapshots, "pinnacle", Market.asian_handicap(0.5), Selection.AWAY) == 1.90
    assert price(snapshots, "pinnacle", Market.asian_handicap(-0.5), Selection.HOME) == 1.95


def test_missing_odds_give_no_snapshots() -> None:
    item = records("odds_fixture_1208100.json", OddsItem)[0].model_copy(update={"bookmakers": []})
    snapshots, stats = map_odds([item], kind=OddsKind.MORNING, taken_at=TAKEN_AT)
    assert snapshots == []
    assert stats.invalid_prices == 0


@given(st.text(max_size=20), st.text(max_size=8))
def test_odds_mapping_never_crashes_on_odd_values(value: str, odd: str) -> None:
    item = OddsItem.model_validate(
        {
            "league": {"id": 39, "season": 2024},
            "fixture": {"id": 1},
            "bookmakers": [
                {
                    "id": 1,
                    "name": "X",
                    "bets": [
                        {"id": 4, "name": name, "values": [{"value": value, "odd": odd}]}
                        for name in ("Asian Handicap", "Goals Over/Under", "Match Winner")
                    ],
                }
            ],
        }
    )
    snapshots, _ = map_odds([item], kind=OddsKind.OPENING, taken_at=TAKEN_AT)
    assert all(s.odds > 1 for s in snapshots)


def test_injuries_and_lineups() -> None:
    absences = map_injuries(records("injuries_fixture_1208100.json", InjuryItem))
    assert [(a.player, a.doubtful) for a in absences] == [
        ("B. Saka", False),
        ("D. Szoboszlai", True),
    ]
    assert absences[0].team == ExternalRef("api-football", "42")
    lineups = map_lineups(records("lineups_fixture_1208100.json", LineupItem))
    assert [lineup.formation for lineup in lineups] == ["4-3-3", "4-2-3-1"]
    assert len(lineups[0].starters) == 11
    assert lineups[0].substitutes == ("Sub One",)
    assert map_lineups(records("lineups_empty.json", LineupItem)) == []


def test_expected_goals() -> None:
    xg = expected_goals(records("statistics_fixture_1208021.json", TeamStatisticsItem))
    assert xg == {ExternalRef("api-football", "33"): 1.73, ExternalRef("api-football", "36"): 0.48}
    assert expected_goals(records("statistics_no_xg.json", TeamStatisticsItem)) == {}


def test_expected_goals_ignores_garbage_and_negatives() -> None:
    item = TeamStatisticsItem.model_validate(
        {
            "team": {"id": 1, "name": "A"},
            "statistics": [
                {"type": "expected_goals", "value": "n/a"},
                {"type": "expected_goals", "value": -1},
            ],
        }
    )
    assert expected_goals([item]) == {}
