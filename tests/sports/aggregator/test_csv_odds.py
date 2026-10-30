"""football-data.co.uk odds loader: parsing, cache and download failures (no real network)."""

import os
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from hypothesis import given
from hypothesis import strategies as st
from tenacity import wait_none

from app.errors import DataError, ExternalServiceError
from app.sports.aggregator.csv_odds import (
    FIXTURES_URL,
    UPCOMING_MAX_AGE,
    OddsCsvLoader,
    bet365_odds,
    parse_odds_csv,
)
from app.sports.aggregator.models import MatchOdds
from app.sports.history import download
from app.sports.history.download import cache_path, create_client, write_atomically

NOW = datetime(2026, 10, 30, 9, tzinfo=UTC)
SEASON_URL = "https://www.football-data.co.uk/mmz4281/2627/E0.csv"
SEASON_CSV = (
    b"Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,B365H,B365D,B365A\n"
    b"E0,22/08/2026,15:00,Man United,Arsenal,1,1,2.40,3.40,2.90\n"
)
FIXTURES_CSV = (
    b"\xef\xbb\xbfDiv,Date,Time,HomeTeam,AwayTeam,B365H,B365D,B365A\n"
    b"E0,31/10/2026,17:30,Arsenal,Chelsea,1.80,3.75,4.50\n"
    b"SP1,31/10/2026,20:00,Real Madrid,Getafe,1.25,6.00,11.00\n"
    b"E0,01/11/2026,,Liverpool,Everton,,,\n"
    b",,,,,,,\n"
)


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(download, "RETRY_WAIT", wait_none())


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with create_client() as client:
        yield client


def loader(http: httpx.AsyncClient, cache: Path) -> OddsCsvLoader:
    return OddsCsvLoader(http, cache, clock=lambda: NOW)


def saved(path: Path, content: bytes, modified: datetime) -> None:
    write_atomically(path, content)
    os.utime(path, (modified.timestamp(), modified.timestamp()))


def test_fixtures_file_rows_keep_division_kickoff_and_prices() -> None:
    parsed = parse_odds_csv(FIXTURES_CSV, season=2026)
    assert parsed.skipped == ()
    arsenal, madrid, derby = parsed.rows
    assert arsenal.division == "E0"
    assert arsenal.day == date(2026, 10, 31)
    assert arsenal.kickoff == time(17, 30)
    assert arsenal.odds == MatchOdds(home_win=1.80, draw=3.75, away_win=4.50)
    assert madrid.division == "SP1"
    assert derby.kickoff is None
    assert derby.odds is None  # the match is listed before prices exist
    assert parsed.of_division("SP1") == (madrid,)


def test_old_spellings_and_a_given_division() -> None:
    content = b"Date,HT,AT,B365H,B365D,B365A\n20/08/00,Chelsea,West Ham,1.5,3.6,6.0\n"
    [row] = parse_odds_csv(content, season=2000, division="E0").rows
    assert (row.division, row.day, row.home_team) == ("E0", date(2000, 8, 20), "Chelsea")


def test_bad_rows_are_reported_not_fatal() -> None:
    content = (
        b"Date,HomeTeam,AwayTeam,B365H,B365D,B365A\n"
        b"31/10/2026,Arsenal,Chelsea,1.8,3.7,4.5\n"  # no division anywhere
    )
    parsed = parse_odds_csv(content, season=2026)
    assert parsed.rows == ()
    assert [skip.reason for skip in parsed.skipped] == ["row has no division"]
    broken = (
        b"Div,Date,HomeTeam,AwayTeam\n"
        b"E0,99/99/2026,Arsenal,Chelsea\n"
        b"E0,31/10/2026,,Chelsea\n"
        b"E0,31/10/2026,Arsenal,Chelsea\n"
    )
    parsed = parse_odds_csv(broken, season=2026)
    assert len(parsed.rows) == 1
    assert len(parsed.skipped) == 2


@pytest.mark.parametrize(
    "content",
    [b"Div,Date,HomeTeam\nE0,31/10/2026,Arsenal\n", b"<html>blocked</html>\n", b""],
)
def test_a_file_without_the_key_columns_is_rejected(content: bytes) -> None:
    with pytest.raises(DataError, match="no column for"):
        parse_odds_csv(content, season=2026)


@pytest.mark.parametrize(
    "cells",
    [
        {"B365H": "", "B365D": "3.4", "B365A": "2.9"},
        {"B365H": "x", "B365D": "3.4", "B365A": "2.9"},
        {"B365H": "1.0", "B365D": "3.4", "B365A": "2.9"},
        {"B365H": "inf", "B365D": "3.4", "B365A": "2.9"},
        {},
    ],
)
def test_unusable_prices_become_none(cells: dict[str, str]) -> None:
    assert bet365_odds(cells) is None


@given(st.binary(max_size=400))
def test_any_bytes_either_parse_or_raise_data_error(content: bytes) -> None:
    try:
        parsed = parse_odds_csv(content, season=2026)
    except DataError:
        return
    assert all(row.home_team and row.away_team for row in parsed.rows)


@given(
    st.lists(
        st.tuples(
            st.floats(min_value=1.01, max_value=1000, allow_nan=False),
            st.floats(min_value=1.01, max_value=1000, allow_nan=False),
            st.floats(min_value=1.01, max_value=1000, allow_nan=False),
        ),
        max_size=5,
    )
)
def test_valid_prices_round_trip(prices: list[tuple[float, float, float]]) -> None:
    lines = [b"Div,Date,HomeTeam,AwayTeam,B365H,B365D,B365A"]
    lines += [
        f"E0,31/10/2026,H{i},A{i},{h!r},{d!r},{a!r}".encode() for i, (h, d, a) in enumerate(prices)
    ]
    parsed = parse_odds_csv(b"\n".join(lines), season=2026)
    assert [
        (r.odds.home_win, r.odds.draw, r.odds.away_win) for r in parsed.rows if r.odds
    ] == prices


@respx.mock
async def test_upcoming_is_downloaded_once_then_served_from_the_cache(
    http: httpx.AsyncClient, tmp_path: Path
) -> None:
    route = respx.get(FIXTURES_URL).mock(return_value=httpx.Response(200, content=FIXTURES_CSV))
    odds = loader(http, tmp_path)
    first = await odds.upcoming()
    second = await odds.upcoming()
    assert len(first.rows) == 3
    assert second == first
    assert route.call_count == 1


@respx.mock
async def test_a_stale_upcoming_file_is_refreshed(http: httpx.AsyncClient, tmp_path: Path) -> None:
    saved(tmp_path / "fixtures.csv", b"Div,Date,HomeTeam,AwayTeam\n", NOW - UPCOMING_MAX_AGE * 2)
    respx.get(FIXTURES_URL).mock(return_value=httpx.Response(200, content=FIXTURES_CSV))
    assert len((await loader(http, tmp_path).upcoming()).rows) == 3


@respx.mock
async def test_a_failed_refresh_falls_back_to_the_cached_copy(
    http: httpx.AsyncClient, tmp_path: Path
) -> None:
    saved(tmp_path / "fixtures.csv", FIXTURES_CSV, NOW - timedelta(days=2))
    respx.get(FIXTURES_URL).mock(return_value=httpx.Response(503))
    assert len((await loader(http, tmp_path).upcoming()).rows) == 3


@respx.mock
async def test_without_a_cached_copy_the_failure_is_raised(
    http: httpx.AsyncClient, tmp_path: Path
) -> None:
    respx.get(FIXTURES_URL).mock(return_value=httpx.Response(200, content=b"<html>no</html>"))
    with pytest.raises(ExternalServiceError, match="did not return a CSV"):
        await loader(http, tmp_path).upcoming()


@respx.mock
async def test_season_file_is_cached_in_the_history_layout(
    http: httpx.AsyncClient, tmp_path: Path
) -> None:
    respx.get(SEASON_URL).mock(return_value=httpx.Response(200, content=SEASON_CSV))
    parsed = await loader(http, tmp_path).season("E0", 2026)
    assert [row.home_team for row in parsed.rows] == ["Man United"]
    assert cache_path(tmp_path, "E0", 2026).is_file()


@respx.mock
async def test_an_unpublished_season_is_empty(http: httpx.AsyncClient, tmp_path: Path) -> None:
    respx.get(SEASON_URL).mock(return_value=httpx.Response(404))
    assert (await loader(http, tmp_path).season("E0", 2026)).rows == ()
