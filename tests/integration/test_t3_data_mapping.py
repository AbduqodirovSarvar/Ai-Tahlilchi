"""T3 QA: API-Football mapping oracles built from hand-made records (not the shared fixtures).

Scores, statuses and Asian-handicap signs decide how bets are settled and which lines are
priced, so each expectation below is derived from the API-Football v3 documentation and the
domain convention (``Market.asian_handicap(line)`` = the *home* team's line), not from the
mapper's own output.
"""

import math
from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.clients.api_football_models import FixtureItem, OddsItem
from app.domain import (
    DomainValidationError,
    FixtureStatus,
    Market,
    OddsKind,
    ResultStatus,
    Selection,
)
from app.sports.football_mapping import HandicapConvention, map_fixture, map_odds, map_result

TAKEN_AT = datetime(2025, 2, 1, 8, 0, tzinfo=UTC)
Pair = tuple[int | None, int | None]


def pair(score: Pair) -> dict[str, int | None]:
    return {"home": score[0], "away": score[1]}


def fixture_item(  # noqa: PLR0913 - one keyword per score period
    code: str,
    *,
    goals: Pair = (None, None),
    halftime: Pair = (None, None),
    fulltime: Pair = (None, None),
    extratime: Pair = (None, None),
    penalty: Pair = (None, None),
) -> FixtureItem:
    return FixtureItem.model_validate(
        {
            "fixture": {
                "id": 9001,
                "date": "2025-02-01T15:00:00+03:00",
                "status": {"short": code},
            },
            "league": {"id": 39, "name": "Premier League", "country": "England", "season": 2024},
            "teams": {"home": {"id": 33, "name": "Home FC"}, "away": {"id": 36, "name": "Away FC"}},
            "goals": pair(goals),
            "score": {
                "halftime": pair(halftime),
                "fulltime": pair(fulltime),
                "extratime": pair(extratime),
                "penalty": pair(penalty),
            },
        }
    )


# --------------------------------------------------------------------------- results


def test_extra_time_and_penalty_matches_settle_on_the_regular_time_score() -> None:
    """A cup tie 1-1 after 90 minutes, 2-1 after extra time: 1X2/totals/BTTS use 1-1."""
    aet = map_result(
        fixture_item("AET", goals=(2, 1), halftime=(0, 1), fulltime=(1, 1), extratime=(1, 0))
    )
    assert aet is not None
    assert (aet.status, aet.home_score, aet.away_score) == (ResultStatus.FINISHED, 1, 1)
    assert (aet.home_ht_score, aet.away_ht_score) == (0, 1)
    # 0-0 after 90 minutes, 5-4 on penalties: the provider's ``goals`` repeat the 120' score.
    pen = map_result(
        fixture_item("PEN", goals=(0, 0), fulltime=(0, 0), extratime=(0, 0), penalty=(5, 4))
    )
    assert pen is not None
    assert (pen.home_score, pen.away_score) == (0, 0)


def test_goals_field_is_never_used_as_a_regular_time_score_after_extra_time() -> None:
    for code in ("AET", "PEN"):
        with pytest.raises(DomainValidationError):
            map_result(fixture_item(code, goals=(3, 2)))  # no fulltime: refuse to guess


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("PST", ResultStatus.POSTPONED),
        ("CANC", ResultStatus.CANCELLED),
        ("AWD", ResultStatus.ABANDONED),
        ("WO", ResultStatus.ABANDONED),
    ],
)
def test_unplayed_matches_carry_no_score_even_if_the_provider_sends_one(
    code: str, status: ResultStatus
) -> None:
    result = map_result(fixture_item(code, goals=(3, 0), fulltime=(3, 0), halftime=(1, 0)))
    assert result is not None
    assert result.status is status
    assert (result.home_score, result.away_score) == (None, None)
    assert (result.home_ht_score, result.away_ht_score) == (None, None)


def test_abandoned_match_is_never_reported_as_finished() -> None:
    result = map_result(fixture_item("ABD", goals=(1, 0), fulltime=(1, 0)))
    assert result is not None
    assert result.status is ResultStatus.ABANDONED
    fixture = map_fixture(fixture_item("ABD", goals=(1, 0), fulltime=(1, 0)))
    assert fixture.status is FixtureStatus.ABANDONED


@pytest.mark.parametrize("code", ["NS", "TBD", "1H", "HT", "2H", "ET", "P", "LIVE", "SUSP", "INT"])
def test_scheduled_and_live_matches_have_no_result(code: str) -> None:
    assert map_result(fixture_item(code, goals=(1, 0), fulltime=(1, 0))) is None


def test_kickoff_offset_is_converted_to_utc() -> None:
    fixture = map_fixture(fixture_item("NS"))
    assert fixture.kickoff == datetime(2025, 2, 1, 12, 0, tzinfo=UTC)
    assert fixture.kickoff.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


def test_half_time_above_full_time_is_dropped_not_stored() -> None:
    result = map_result(fixture_item("FT", goals=(1, 0), halftime=(2, 0), fulltime=(1, 0)))
    assert result is not None
    assert (result.home_score, result.home_ht_score) == (1, None)


@given(
    code=st.sampled_from(["FT", "AET", "PEN", "PST", "CANC", "ABD", "AWD", "NS", "1H"]),
    goals=st.tuples(st.none() | st.integers(0, 12), st.none() | st.integers(0, 12)),
    fulltime=st.tuples(st.none() | st.integers(0, 12), st.none() | st.integers(0, 12)),
    halftime=st.tuples(st.none() | st.integers(0, 12), st.none() | st.integers(0, 12)),
)
def test_result_mapping_is_total_and_consistent(
    code: str, goals: Pair, fulltime: Pair, halftime: Pair
) -> None:
    try:
        result = map_result(fixture_item(code, goals=goals, fulltime=fulltime, halftime=halftime))
    except DomainValidationError:
        return  # a finished match without a usable score is refused, never invented
    if result is None:
        assert code in {"NS", "1H"}
        return
    if result.status is ResultStatus.FINISHED:
        assert result.home_score is not None
        assert result.away_score is not None
        if code in {"AET", "PEN"}:
            assert (result.home_score, result.away_score) == fulltime
    if result.home_ht_score is not None and result.home_score is not None:
        assert result.home_ht_score <= result.home_score


# --------------------------------------------------------------------------- odds


def odds_item(*bets: tuple[str, list[tuple[str, str | float]]]) -> OddsItem:
    return OddsItem.model_validate(
        {
            "league": {"id": 39, "season": 2024},
            "fixture": {"id": 9001},
            "bookmakers": [
                {
                    "id": 4,
                    "name": "Pinnacle",
                    "bets": [
                        {
                            "id": index,
                            "name": name,
                            "values": [{"value": value, "odd": odd} for value, odd in values],
                        }
                        for index, (name, values) in enumerate(bets, start=1)
                    ],
                }
            ],
        }
    )


def quotes(
    item: OddsItem, convention: HandicapConvention = HandicapConvention.HOME_LINE
) -> dict[tuple[Market, Selection], float]:
    snapshots, _ = map_odds([item], kind=OddsKind.MORNING, taken_at=TAKEN_AT, convention=convention)
    return {(s.market, s.selection): s.odds for s in snapshots}


def test_asian_handicap_sign_under_the_home_line_convention() -> None:
    """HOME_LINE: the number is the home team's line on both the Home and the Away value."""
    mapped = quotes(
        odds_item(
            (
                "Asian Handicap",
                [
                    ("Home -1.5", 2.30),
                    ("Away -1.5", 1.65),
                    ("Home +0.25", 1.70),
                    ("Away +0.25", 2.2),
                ],
            )
        )
    )
    assert mapped == {
        (Market.asian_handicap(-1.5), Selection.HOME): 2.30,
        (Market.asian_handicap(-1.5), Selection.AWAY): 1.65,
        (Market.asian_handicap(0.25), Selection.HOME): 1.70,
        (Market.asian_handicap(0.25), Selection.AWAY): 2.2,
    }


def test_asian_handicap_sign_under_the_own_line_convention() -> None:
    """OWN_LINE: ``Away +1.5`` is the away team's own line, i.e. home line -1.5."""
    mapped = quotes(
        odds_item(
            (
                "Asian Handicap",
                [
                    ("Home -1.5", 2.30),
                    ("Away +1.5", 1.65),
                    ("Home +0.25", 1.70),
                    ("Away -0.25", 2.2),
                ],
            )
        ),
        HandicapConvention.OWN_LINE,
    )
    assert mapped == {
        (Market.asian_handicap(-1.5), Selection.HOME): 2.30,
        (Market.asian_handicap(-1.5), Selection.AWAY): 1.65,
        (Market.asian_handicap(0.25), Selection.HOME): 1.70,
        (Market.asian_handicap(0.25), Selection.AWAY): 2.2,
    }


def test_the_two_conventions_disagree_only_on_the_away_sign() -> None:
    item = odds_item(("Asian Handicap", [("Home -0.5", 1.9), ("Away -0.5", 1.95)]))
    home_line = quotes(item)
    own_line = quotes(item, HandicapConvention.OWN_LINE)
    assert (Market.asian_handicap(-0.5), Selection.HOME) in home_line
    assert (Market.asian_handicap(-0.5), Selection.HOME) in own_line
    assert (Market.asian_handicap(-0.5), Selection.AWAY) in home_line
    assert (Market.asian_handicap(0.5), Selection.AWAY) in own_line
    assert (Market.asian_handicap(-0.5), Selection.AWAY) not in own_line


def test_zero_handicap_has_no_sign() -> None:
    for convention in HandicapConvention:
        mapped = quotes(
            odds_item(("Asian Handicap", [("Home 0", 1.8), ("Away 0", 2.0)])), convention
        )
        assert set(mapped) == {
            (Market.asian_handicap(0), Selection.HOME),
            (Market.asian_handicap(0), Selection.AWAY),
        }


def test_unknown_bet_types_are_ignored_and_reported() -> None:
    item = odds_item(
        ("Match Winner", [("Home", 2.0), ("Draw", 3.4), ("Away", 3.8)]),
        ("Exact Score", [("1:0", 8.0)]),
        ("Correct Score First Half", [("0:0", 3.0)]),
        ("Asian Handicap First Half", [("Home -0.5", 2.2)]),
        ("Goals Over/Under First Half", [("Over 0.5", 1.5)]),
        ("Home/Away", [("Home", 1.4), ("Away", 2.9)]),
        ("Draw No Bet", [("Home", 1.5)]),
        ("", [("Home", 1.5)]),
    )
    snapshots, stats = map_odds([item], kind=OddsKind.MORNING, taken_at=TAKEN_AT)
    assert {s.market.type.value for s in snapshots} == {"1x2"}
    assert len(snapshots) == 3
    assert {"Exact Score", "Asian Handicap First Half", "Draw No Bet", "Home/Away"} <= (
        stats.unknown_bets
    )


@pytest.mark.parametrize(
    "bad", ["nan", "inf", "-inf", "1e999", "-2", "0", "1.0", "", "abc", "1,85"]
)
def test_unusable_prices_never_become_snapshots(bad: str) -> None:
    mapped = quotes(odds_item(("Match Winner", [("Home", bad), ("Draw", 3.4), ("Away", 3.8)])))
    assert (Market.match_result(), Selection.HOME) not in mapped
    assert all(math.isfinite(price) and price > 1 for price in mapped.values())


@given(
    bet=st.text(max_size=30),
    value=st.text(max_size=30),
    odd=st.one_of(st.text(max_size=12), st.floats(allow_nan=True, allow_infinity=True)),
)
def test_odds_mapping_never_crashes_and_never_emits_bad_prices(
    bet: str, value: str, odd: str | float
) -> None:
    snapshots, _ = map_odds(
        [odds_item((bet, [(value, odd)]), ("Asian Handicap", [(value, odd)]))],
        kind=OddsKind.CLOSING,
        taken_at=TAKEN_AT,
    )
    for snapshot in snapshots:
        assert math.isfinite(snapshot.odds)
        assert snapshot.odds > 1


def test_first_price_of_a_selection_wins_when_a_bookmaker_repeats_it() -> None:
    mapped = quotes(odds_item(("Match Winner", [("Home", 2.0), ("Home", 9.9), ("Draw", 3.4)])))
    assert mapped[(Market.match_result(), Selection.HOME)] == 2.0
