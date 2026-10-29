"""Parsing of football-data.co.uk CSVs (app.sports.history.football_data)."""

from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain import (
    BOOKMAKER_MARKET_AVERAGE,
    BOOKMAKER_PINNACLE,
    Fixture,
    Market,
    MarketType,
    OddsKind,
    OddsSnapshot,
    ResultStatus,
    Selection,
)
from app.errors import DataError
from app.sports.history.football_data import (
    EARLY_ODDS_LEAD,
    LEAGUES,
    ParsedSeason,
    RowError,
    _expand_two_digit_year,
    decode_csv,
    league_for,
    parse_date,
    parse_decimal,
    parse_goals,
    parse_season_csv,
    parse_time,
    season_code,
)

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "football_data"


def parse_fixture_file(relative: str, code: str, season: int) -> ParsedSeason:
    content = (FIXTURES_DIR / relative).read_bytes()
    return parse_season_csv(content, league=league_for(code), season=season)


@pytest.fixture(scope="module")
def premier_league() -> ParsedSeason:
    return parse_fixture_file("2425/E0.csv", "E0", 2024)


def by_home(parsed: ParsedSeason, home: str) -> Fixture:
    return next(fixture for fixture in parsed.batch.fixtures if fixture.home.name == home)


def prices(parsed: ParsedSeason, fixture: Fixture, **match: object) -> list[OddsSnapshot]:
    return [
        snapshot
        for snapshot in parsed.batch.odds
        if snapshot.fixture == fixture.ref
        and all(getattr(snapshot, key) == value for key, value in match.items())
    ]


def test_valid_rows_become_fixtures_and_bad_rows_are_reported(premier_league: ParsedSeason) -> None:
    assert premier_league.rows == 6  # the blank line is not a row
    assert [f.home.name for f in premier_league.batch.fixtures] == [
        "Man United",
        "Ipswich",
        "Nott'm Forest",
        "Arsenal",
    ]
    reasons = {skipped.line: skipped.reason for skipped in premier_league.skipped}
    assert reasons == {5: "full-time score is missing", 7: "invalid date '32/08/2024'"}


def test_uk_kickoff_is_converted_to_utc(premier_league: ParsedSeason) -> None:
    fixture = by_home(premier_league, "Man United")
    assert fixture.kickoff == datetime(2024, 8, 16, 19, 0, tzinfo=UTC)  # 20:00 BST
    assert fixture.season == 2024
    assert fixture.ref.external_id == "E0/2024/Man United/Fulham"


def test_missing_kickoff_time_uses_default_and_is_counted(premier_league: ParsedSeason) -> None:
    assert premier_league.missing_kickoff_time == 1
    assert by_home(premier_league, "Arsenal").kickoff == datetime(2024, 8, 24, 11, tzinfo=UTC)


def test_results_include_half_time_scores(premier_league: ParsedSeason) -> None:
    result = next(
        r
        for r in premier_league.batch.results
        if r.fixture == by_home(premier_league, "Nott'm Forest").ref
    )
    assert result.status is ResultStatus.FINISHED
    assert (result.home_score, result.away_score) == (1, 1)
    assert (result.home_ht_score, result.away_ht_score) == (1, 0)


def test_all_markets_of_a_fully_quoted_row(premier_league: ParsedSeason) -> None:
    fixture = by_home(premier_league, "Man United")
    snapshots = prices(premier_league, fixture)
    assert len(snapshots) == 8 * 3 + 8 * 2 + 8 * 2
    closing = prices(premier_league, fixture, bookmaker=BOOKMAKER_PINNACLE, kind=OddsKind.CLOSING)
    assert {(s.market, s.selection): s.odds for s in closing} == {
        (Market.match_result(), Selection.HOME): 1.57,
        (Market.match_result(), Selection.DRAW): 4.17,
        (Market.match_result(), Selection.AWAY): 5.22,
        (Market.totals(2.5), Selection.OVER): 1.81,
        (Market.totals(2.5), Selection.UNDER): 2.06,
        (Market.asian_handicap(-1), Selection.HOME): 2.06,
        (Market.asian_handicap(-1), Selection.AWAY): 1.86,
    }
    assert {s.taken_at for s in closing} == {fixture.kickoff}
    early = prices(premier_league, fixture, kind=OddsKind.OPENING)
    assert {s.taken_at for s in early} == {fixture.kickoff - EARLY_ODDS_LEAD}


def test_missing_group_is_absent_and_other_groups_kept(premier_league: ParsedSeason) -> None:
    fixture = by_home(premier_league, "Ipswich")
    pinnacle_closing_1x2 = [
        s
        for s in prices(
            premier_league, fixture, bookmaker=BOOKMAKER_PINNACLE, kind=OddsKind.CLOSING
        )
        if s.market.type is MarketType.MATCH_RESULT
    ]
    assert pinnacle_closing_1x2 == []
    average_closing = prices(
        premier_league, fixture, bookmaker=BOOKMAKER_MARKET_AVERAGE, kind=OddsKind.CLOSING
    )
    assert len(average_closing) == 3 + 2 + 2


def test_quarter_handicap_line_is_kept(premier_league: ParsedSeason) -> None:
    fixture = by_home(premier_league, "Ipswich")
    handicap = prices(premier_league, fixture, bookmaker=BOOKMAKER_PINNACLE, kind=OddsKind.CLOSING)
    assert {s.market for s in handicap} == {Market.totals(2.5), Market.asian_handicap(1.75)}


def test_invalid_price_drops_the_whole_group(premier_league: ParsedSeason) -> None:
    fixture = by_home(premier_league, "Nott'm Forest")
    closing_1x2 = [
        s
        for s in prices(
            premier_league, fixture, bookmaker=BOOKMAKER_PINNACLE, kind=OddsKind.CLOSING
        )
        if s.market.type is MarketType.MATCH_RESULT
    ]
    assert closing_1x2 == []
    assert premier_league.incomplete_price_groups == 1


def test_old_layout_in_windows_1252_with_two_digit_years() -> None:
    parsed = parse_fixture_file("2122/SP1.csv", "SP1", 2021)
    assert [f.home.name for f in parsed.batch.fixtures] == ["Valencia", "Cádiz", "Alavés"]
    assert by_home(parsed, "Cádiz").home.normalized_name == "cadiz"
    assert by_home(parsed, "Valencia").kickoff.date() == date(2021, 8, 13)
    assert parsed.missing_kickoff_time == 3
    assert [s.reason for s in parsed.skipped] == ["row belongs to division 'SP2', not 'SP1'"]
    assert len(parsed.batch.odds) == 3 * 3 * 3


def test_utf8_bom_and_old_column_names() -> None:
    parsed = parse_fixture_file("2324/D1.csv", "D1", 2023)
    assert [(f.home.name, f.away.name) for f in parsed.batch.fixtures] == [
        ("Werder Bremen", "Bayern Munich"),
        ("Düsseldorf", "Köln"),
    ]
    assert [r.home_score for r in parsed.batch.results] == [0, 2]
    assert all(s.kind is OddsKind.CLOSING for s in parsed.batch.odds)


def test_duplicate_rows_are_reported() -> None:
    content = (
        b"Div,Date,HomeTeam,AwayTeam,FTHG,FTAG\nE0,16/08/2024,A,B,1,0\nE0,16/08/2024,A,B,1,0\n"
    )
    parsed = parse_season_csv(content, league=league_for("E0"), season=2024)
    assert len(parsed.batch.fixtures) == 1
    assert parsed.skipped[0].reason.startswith("duplicate fixture")


def test_ragged_rows_do_not_break_parsing() -> None:
    content = (
        b"Date,HomeTeam,AwayTeam,FTHG,FTAG\n"
        b"16/08/2024,A,B,1\n"  # missing cell
        b"17/08/2024,C,D,2,2,extra,cells\n"  # overflow cells
        b"18/08/2024,,F,0,0\n"  # empty team
    )
    parsed = parse_season_csv(content, league=league_for("E0"), season=2024)
    assert [f.home.name for f in parsed.batch.fixtures] == ["C"]
    assert [s.reason for s in parsed.skipped] == [
        "full-time score is missing",
        "team name is empty",
    ]


@pytest.mark.parametrize(
    "content",
    [b"", b"<!DOCTYPE html><html>Blocked</html>", b"Div,Date,HomeTeam\nE0,16/08/2024,A\n"],
)
def test_files_without_required_columns_are_rejected(content: bytes) -> None:
    with pytest.raises(DataError, match="missing columns"):
        parse_season_csv(content, league=league_for("E0"), season=2024)


def test_structurally_broken_csv_is_rejected_as_a_whole() -> None:
    oversized_field = b'"' + b"x" * 200_000 + b'"'
    content = b"Date,HomeTeam,AwayTeam,FTHG,FTAG\n" + oversized_field + b",A,B,1,0\n"
    with pytest.raises(DataError, match="malformed CSV"):
        parse_season_csv(content, league=league_for("E0"), season=2024)


def test_unknown_league_code_is_rejected() -> None:
    with pytest.raises(DataError, match="unsupported football-data division"):
        league_for("XX")


def test_catalogue_contains_the_default_leagues() -> None:
    assert set(LEAGUES) == {"E0", "E1", "SP1", "D1", "I1", "F1", "N1", "P1", "B1", "T1"}
    assert league_for("T1").country == "Turkey"


@pytest.mark.parametrize(("season", "code"), [(2024, "2425"), (1999, "9900"), (2009, "0910")])
def test_season_code(season: int, code: str) -> None:
    assert season_code(season) == code


@pytest.mark.parametrize(
    ("text", "season", "expected"),
    [
        ("16/08/2024", 2024, date(2024, 8, 16)),
        ("1/8/24", 2024, date(2024, 8, 1)),
        ("07/08/99", 1999, date(1999, 8, 7)),
        ("15/01/00", 1999, date(2000, 1, 15)),
        ("31/12/99", 2099, date(2099, 12, 31)),
        ("26/07/2020", 2019, date(2020, 7, 26)),  # COVID-extended season, inside the margin
    ],
)
def test_parse_date_formats(text: str, season: int, expected: date) -> None:
    assert parse_date(text, season=season) == expected


@pytest.mark.parametrize("text", ["", "2024-08-16", "16.08.2024", "31/02/2024", "16/08/202"])
def test_parse_date_rejects_other_text(text: str) -> None:
    with pytest.raises(RowError):
        parse_date(text, season=2024)


@pytest.mark.parametrize(
    ("two_digits", "season", "year"), [(99, 2001, 1999), (1, 1999, 2001), (24, 2024, 2024)]
)
def test_two_digit_year_takes_the_nearest_century(two_digits: int, season: int, year: int) -> None:
    assert _expand_two_digit_year(two_digits, season) == year


@pytest.mark.parametrize("text", ["16/08/2023", "01/10/2025", "16/08/14"])
def test_parse_date_rejects_dates_outside_the_season(text: str) -> None:
    with pytest.raises(RowError, match="outside season 2024/2025"):
        parse_date(text, season=2024)


@given(st.dates(min_value=date(1950, 7, 1), max_value=date(2150, 6, 30)))
def test_parse_date_round_trips_both_year_styles(day: date) -> None:
    season = day.year if day.month >= 7 else day.year - 1
    assert parse_date(day.strftime("%d/%m/%Y"), season=season) == day
    two_digit = f"{day.day:02d}/{day.month:02d}/{day.year % 100:02d}"
    assert parse_date(two_digit, season=season) == day


@pytest.mark.parametrize(
    ("text", "expected"),
    [("20:00", time(20)), ("9.30", time(9, 30)), ("", None), ("25:00", None), ("tbc", None)],
)
def test_parse_time(text: str, expected: time | None) -> None:
    assert parse_time(text) == expected


@pytest.mark.parametrize(("text", "expected"), [("2", 2), ("0", 0), ("3.0", 3), ("", None)])
def test_parse_goals(text: str, expected: int | None) -> None:
    assert parse_goals(text) == expected


@pytest.mark.parametrize("text", ["-1", "1.5", "two"])
def test_parse_goals_rejects_non_counts(text: str) -> None:
    with pytest.raises(RowError):
        parse_goals(text)


def test_parse_decimal_rejects_empty_and_text() -> None:
    assert parse_decimal("1.85") == 1.85
    for text in ("", "n/a"):
        with pytest.raises(RowError):
            parse_decimal(text)


@given(st.text())
def test_decode_csv_never_fails(text: str) -> None:
    for encoding in ("utf-8", "utf-16"):
        assert isinstance(decode_csv(text.encode(encoding, errors="replace")), str)


@given(st.binary(max_size=2000))
def test_parser_never_crashes_on_arbitrary_bytes(content: bytes) -> None:
    try:
        parsed = parse_season_csv(content, league=league_for("E0"), season=2024)
    except DataError:
        return
    assert len(parsed.batch.fixtures) + len(parsed.skipped) <= parsed.rows


CARDS_CSV = (
    "Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,HTHG,HTAG,Referee,HY,AY,HR,AR\n"
    "E0,16/08/2024,20:00,Man United,Fulham,1,0,0,0,R Jones,2,3,0,1\n"
    "E0,17/08/2024,12:30,Ipswich,Liverpool,0,2,0,0,,x,1,,\n"
)


def test_cards_and_referee_are_imported_and_a_bad_card_cell_keeps_the_result() -> None:
    parsed = parse_season_csv(CARDS_CSV.encode(), league=league_for("E0"), season=2024)
    first, second = parsed.batch.results
    assert (first.home_yellow, first.away_yellow, first.home_red, first.away_red) == (2, 3, 0, 1)
    assert first.referee == "R Jones"
    assert (second.home_score, second.away_score) == (0, 2)  # still imported
    assert (second.home_yellow, second.away_yellow, second.home_red) == (None, 1, None)
    assert second.referee is None
