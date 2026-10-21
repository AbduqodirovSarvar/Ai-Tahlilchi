"""QA (T2): football-data history import end to end, from CSV bytes to stored rows.

Expected numbers are counted by hand from the CSV files, never derived from the code under
test, and stored rows are read back with plain ``sqlite3`` (independent of the ORM).
"""

import os
import re
import shutil
import sqlite3
import time
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx

from app.sports.history.football_data import league_for, parse_season_csv
from tests.integration.history_helpers import (
    FOOTBALL_DATA_FIXTURES,
    ODDS_PER_FULL_ROW,
    ImportWorkspace,
    full_row,
    sqlite_connection,
)

# ----------------------------------------------------------------------------- oracle
# tests/fixtures/football_data, counted cell by cell:
# E0 2024/25: Man United-Fulham (all 24 groups: 56 prices), Ipswich-Liverpool (Pinnacle
#   closing 1X2 empty: 53), Nott'm Forest-Bournemouth (PSCD = 1.00 drops that group: 53),
#   Arsenal-Aston Villa (only Pinnacle closing 1X2: 3). Chelsea-Man City (no score), the
#   blank line and 32/08/2024 are not imported.
# SP1 2021/22: 3 rows x (B365, PS, PSC 1X2) = 27; the SP2 row is rejected.
# D1 2023/24: 2 rows x PSC 1X2 = 6.
EXPECTED_TABLES = {"leagues": 3, "teams": 8 + 6 + 4, "fixtures": 4 + 3 + 2, "odds_snapshots": 198}
EXPECTED_BY_BOOKMAKER_KIND = {
    ("bet365", "closing"): 3 * 7,
    ("bet365", "opening"): 3 * 7 + 9,
    ("market_avg", "closing"): 3 * 7,
    ("market_avg", "opening"): 3 * 7,
    ("market_max", "closing"): 3 * 7,
    ("market_max", "opening"): 3 * 7,
    ("pinnacle", "closing"): 7 + 4 + 4 + 3 + 9 + 6,
    ("pinnacle", "opening"): 3 * 7 + 9,
}
EXPECTED_BY_MARKET = {"1x2": 24 + 21 + 21 + 3 + 27 + 6, "totals": 3 * 16, "asian_handicap": 3 * 16}
EXPECTED_FIXTURES_PER_LEAGUE = {("D1", 2023): 2, ("E0", 2024): 4, ("SP1", 2021): 3}
EXPECTED_PINNACLE_CLOSING_1X2 = {("D1", 2023): 2, ("E0", 2024): 2, ("SP1", 2021): 3}
SEASON_2023_E0_URL = "https://www.football-data.co.uk/mmz4281/2324/E0.csv"

IMPORT_10K_CEILING_SECONDS = 240


@pytest.fixture
def committed(workspace: ImportWorkspace) -> ImportWorkspace:
    """Workspace whose cache holds the committed fixture CSVs."""
    shutil.copytree(FOOTBALL_DATA_FIXTURES, workspace.cache_dir, dirs_exist_ok=True)
    return workspace


def import_committed(workspace: ImportWorkspace) -> int:
    return workspace.run("E0,SP1,D1", "2021-2024")


# ----------------------------------------------------------------------------- committed fixtures


def test_offline_import_of_committed_fixtures_stores_exact_counts(
    committed: ImportWorkspace,
) -> None:
    assert import_committed(committed) == 0

    assert committed.counts() == EXPECTED_TABLES
    assert (
        committed.grouped("SELECT bookmaker, kind, count(*) FROM odds_snapshots GROUP BY 1, 2")
        == EXPECTED_BY_BOOKMAKER_KIND
    )
    assert (
        committed.grouped("SELECT market, count(*) FROM odds_snapshots GROUP BY 1")
        == EXPECTED_BY_MARKET
    )
    assert (
        committed.grouped(
            "SELECT l.external_id, f.season, count(*) FROM fixtures f"
            " JOIN leagues l ON l.id = f.league_id GROUP BY 1, 2"
        )
        == EXPECTED_FIXTURES_PER_LEAGUE
    )
    assert (
        committed.grouped(
            "SELECT l.external_id, f.season, count(DISTINCT f.id) FROM fixtures f"
            " JOIN leagues l ON l.id = f.league_id JOIN odds_snapshots o ON o.fixture_id = f.id"
            " WHERE o.bookmaker = 'pinnacle' AND o.kind = 'closing' AND o.market = '1x2'"
            " GROUP BY 1, 2"
        )
        == EXPECTED_PINNACLE_CLOSING_1X2
    )
    names = {name for (name,) in committed.query("SELECT name FROM teams")}
    # Source spelling survives Windows-1252 (SP1) and UTF-8 with BOM (D1) decoding.
    assert {"Cádiz", "Alavés", "Düsseldorf", "Köln", "Nott'm Forest"} <= names
    assert "Chelsea" not in names  # its only row had no score


def test_committed_fixture_values_survive_the_round_trip(committed: ImportWorkspace) -> None:
    import_committed(committed)

    # 20:00 BST on 16 Aug 2024 is 19:00 UTC; FT 1-0, HT 0-0.
    assert committed.fixture("/Man United/Fulham") == (
        "2024-08-16 19:00:00.000000",
        2024,
        "finished",
        1,
        0,
        0,
        0,
    )
    # No Time value or column: noon London (11:00 UTC in summer) keeps the UTC date.
    assert committed.fixture("/Arsenal/Aston Villa")[0] == "2024-08-24 11:00:00.000000"
    assert committed.fixture("/Valencia/Getafe")[:2] == ("2021-08-13 11:00:00.000000", 2021)
    assert committed.prices(
        "/Werder Bremen/Bayern Munich", bookmaker="pinnacle", market="1x2", kind="closing"
    ) == {("home", 0.0, 8.5), ("draw", 0.0, 6.0), ("away", 0.0, 1.3)}
    # Closing prices are taken at kick-off, early prices one day before.
    taken = committed.query(
        "SELECT DISTINCT o.kind, o.taken_at FROM odds_snapshots o JOIN fixtures f"
        " ON f.id = o.fixture_id WHERE f.external_id LIKE '%/Man United/Fulham'"
    )
    assert dict(taken) == {
        "closing": "2024-08-16 19:00:00.000000",
        "opening": "2024-08-15 19:00:00.000000",
    }


def test_second_import_changes_nothing(committed: ImportWorkspace) -> None:
    every_price = (
        "SELECT id, fixture_id, bookmaker, market, selection, line, kind, taken_at, odds"
        " FROM odds_snapshots ORDER BY id"
    )
    import_committed(committed)
    first_counts, first_prices = committed.counts(), committed.query(every_price)

    assert import_committed(committed) == 0

    assert committed.counts() == first_counts
    assert committed.query(every_price) == first_prices  # same ids: updated in place


# ----------------------------------------------------------------------------- corrections


def test_reimport_after_score_and_price_correction_updates_in_place(
    workspace: ImportWorkspace,
) -> None:
    rows = [full_row("Arsenal", "Forest", score=(2, 1)), full_row("Bournemouth", "West Ham")]
    workspace.write_season("E0", 2023, rows)
    workspace.run("E0", "2023")
    before = workspace.counts()

    rows[0] |= {"FTHG": "3", "HTHG": "1", "PSCH": "2.15", "AHCh": "-0.75", "PCAHH": "1.99"}
    workspace.write_season("E0", 2023, rows)
    assert workspace.run("E0", "2023") == 0

    assert before == {
        "leagues": 1,
        "teams": 4,
        "fixtures": 2,
        "odds_snapshots": 2 * ODDS_PER_FULL_ROW,
    }
    # Same fixtures; the new closing handicap line adds 4 groups x 2 prices (see below).
    assert workspace.counts() == before | {"odds_snapshots": 2 * ODDS_PER_FULL_ROW + 8}
    assert workspace.fixture("/Arsenal/Forest")[3:] == (3, 1, 1, 0)
    assert ("home", 0.0, 2.15) in workspace.prices(
        "/Arsenal/Forest", bookmaker="pinnacle", market="1x2", kind="closing"
    )
    closing_handicap = workspace.prices(
        "/Arsenal/Forest", bookmaker="pinnacle", market="asian_handicap", kind="closing"
    )
    # A corrected handicap line is another market: the -0.75 prices are added and the
    # superseded -0.5 prices stay (ADR 0002: snapshots are never deleted).
    assert {("home", -0.75, 1.99), ("home", -0.5, 1.9)} <= closing_handicap


def test_reimport_after_kickoff_time_correction_does_not_duplicate_odds(
    workspace: ImportWorkspace,
) -> None:
    row = full_row("Arsenal", "Forest", when="12/08/2023 17:30")
    workspace.write_season("E0", 2023, [row])
    workspace.run("E0", "2023")

    workspace.write_season("E0", 2023, [row | {"Time": "12:30"}])  # the source corrects it
    workspace.run("E0", "2023")

    assert workspace.fixture("/Arsenal/Forest")[0] == "2023-08-12 11:30:00.000000"
    assert workspace.counts()["odds_snapshots"] == ODDS_PER_FULL_ROW


def test_reimport_after_match_date_correction_does_not_duplicate_the_fixture(
    workspace: ImportWorkspace,
) -> None:
    row = full_row("Arsenal", "Forest", when="12/08/2023 15:00")
    workspace.write_season("E0", 2023, [row])
    workspace.run("E0", "2023")

    workspace.write_season("E0", 2023, [row | {"Date": "13/08/2023"}])
    workspace.run("E0", "2023")

    assert workspace.counts()["fixtures"] == 1


# ----------------------------------------------------------------------------- time


@pytest.mark.parametrize(
    ("when", "expected_utc"),
    [
        ("28/10/2023 15:00", "2023-10-28 14:00"),  # last Saturday of BST (UTC+1)
        ("29/10/2023 15:00", "2023-10-29 15:00"),  # first day of GMT
        ("29/10/2023 01:30", "2023-10-29 00:30"),  # repeated hour: first (BST) occurrence
        ("30/03/2024 15:00", "2024-03-30 15:00"),  # last day of GMT
        ("31/03/2024 15:00", "2024-03-31 14:00"),  # first day of BST
        ("31/03/2024", "2024-03-31 11:00"),  # no time: noon London, same UTC date
        ("31/12/2023 23:30", "2023-12-31 23:30"),  # late winter kick-off keeps its date
    ],
)
def test_uk_kickoff_is_stored_in_utc_across_dst_boundaries(
    workspace: ImportWorkspace, when: str, expected_utc: str
) -> None:
    workspace.write_season("E0", 2023, [full_row("Arsenal", "Forest", when=when)])

    assert workspace.run("E0", "2023") == 0

    stored = workspace.fixture("/Arsenal/Forest")[0]
    assert stored == f"{expected_utc}:00.000000"
    closing = workspace.query("SELECT DISTINCT taken_at FROM odds_snapshots WHERE kind = 'closing'")
    assert closing == [(stored,)]


def test_season_crossing_the_new_year_stays_one_season(workspace: ImportWorkspace) -> None:
    workspace.write_season(
        "E0",
        2024,
        [
            full_row("Man United", "Fulham", when="16/08/2024 20:00"),
            full_row("Chelsea", "Arsenal", when="31/12/24 20:00"),
            full_row("Arsenal", "Chelsea", when="01/01/25 12:30"),
            full_row("Fulham", "Man United", when="25/05/2025 16:00"),
        ],
    )

    assert workspace.run("E0", "2024") == 0

    assert workspace.grouped("SELECT season, count(*) FROM fixtures GROUP BY 1") == {2024: 4}
    assert workspace.fixture("E0/2024/Chelsea/Arsenal")[0] == ("2024-12-31 20:00:00.000000")
    assert workspace.fixture("E0/2024/Arsenal/Chelsea")[0] == ("2025-01-01 12:30:00.000000")
    # The reverse fixture of a pairing in the same season is another match (BST in May).
    assert workspace.fixture("E0/2024/Fulham/Man United")[0] == ("2025-05-25 15:00:00.000000")


def test_two_digit_years_of_a_1999_season_stay_in_the_1990s(workspace: ImportWorkspace) -> None:
    workspace.write_season("E0", 1999, [full_row("Arsenal", "Leicester", when="07/08/99 15:00")])

    assert workspace.run("E0", "1999") == 0

    assert str(workspace.fixture("/Arsenal/Leicester")[0]).startswith("1999-08-07")


# ----------------------------------------------------------------------------- handicap sign


def test_asian_handicap_line_is_stored_from_the_home_side(workspace: ImportWorkspace) -> None:
    underdog_at_home = full_row("Luton", "Man City", line="1.75") | {
        "AHCh": "2.0",
        "PAHH": "1.95",
        "PAHA": "1.97",
        "PCAHH": "1.88",
        "PCAHA": "2.04",
    }
    workspace.write_season("E0", 2023, [underdog_at_home])
    workspace.write_season(
        "E0", 2022, [full_row("Man City", "Luton", when="06/08/2022 15:00", line="-1.0")]
    )

    assert workspace.run("E0", "2022-2023") == 0

    # Luton +1.75 at home; the away price is quoted on the same (home) line, not negated.
    assert workspace.prices(
        "/Luton/Man City", bookmaker="pinnacle", market="asian_handicap", kind="opening"
    ) == {("home", 1.75, 1.95), ("away", 1.75, 1.97)}
    assert workspace.prices(
        "/Luton/Man City", bookmaker="pinnacle", market="asian_handicap", kind="closing"
    ) == {("home", 2.0, 1.88), ("away", 2.0, 2.04)}
    favourite = workspace.prices(
        "/Man City/Luton", bookmaker="bet365", market="asian_handicap", kind="opening"
    )
    assert {line for _, line, _ in favourite} == {-1.0}


# ----------------------------------------------------------------------------- odd files


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "1e400", "1,9", "—"])
def test_non_finite_or_garbled_cells_drop_only_their_group(
    workspace: ImportWorkspace, bad: str
) -> None:
    workspace.write_season(
        "E0",
        2023,
        [
            full_row("Arsenal", "Forest") | {"PSCD": bad},  # one Pinnacle closing 1X2 price
            full_row("Chelsea", "Luton") | {"AHCh": bad},  # the closing handicap line
        ],
    )

    assert workspace.run("E0", "2023") == 0

    assert workspace.counts()["fixtures"] == 2
    assert (
        workspace.prices("/Arsenal/Forest", bookmaker="pinnacle", market="1x2", kind="closing")
        == set()
    )
    assert (
        len(workspace.prices("/Arsenal/Forest", bookmaker="pinnacle", market="1x2", kind="opening"))
        == 3
    )
    closing_handicap = workspace.query(
        "SELECT count(*) FROM odds_snapshots o JOIN fixtures f ON f.id = o.fixture_id"
        " WHERE f.external_id LIKE '%/Chelsea/Luton' AND o.market = 'asian_handicap'"
        " AND o.kind = 'closing'"
    )
    assert closing_handicap == [(0,)]
    assert workspace.counts()["odds_snapshots"] == 2 * ODDS_PER_FULL_ROW - 3 - 4 * 2


def test_unquoted_handicap_bookmakers_are_not_counted_as_incomplete() -> None:
    content = (
        b"Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,AHh,PAHH,PAHA\nE0,12/08/23,A,B,1,0,-0.5,1.9,2.0\n"
    )

    parsed = parse_season_csv(content, league=league_for("E0"), season=2023)

    assert len(parsed.batch.odds) == 2
    assert parsed.incomplete_price_groups == 0  # B365/Max/Avg handicap are simply absent


def test_duplicate_rows_in_one_file_are_stored_once(workspace: ImportWorkspace) -> None:
    row = full_row("Arsenal", "Forest", score=(2, 1))
    workspace.write_season("E0", 2023, [row, row, row | {"FTHG": "5"}])

    assert workspace.run("E0", "2023") == 0

    assert workspace.counts() == {
        "leagues": 1,
        "teams": 2,
        "fixtures": 1,
        "odds_snapshots": ODDS_PER_FULL_ROW,
    }
    assert workspace.fixture("/Arsenal/Forest")[3] == 2  # the first row wins


def test_empty_file_fails_alone_and_header_only_file_imports_nothing(
    workspace: ImportWorkspace,
) -> None:
    empty = workspace.season_file("E0", 2023)
    empty.parent.mkdir(parents=True)
    empty.write_bytes(b"")
    workspace.write_season("E1", 2023, [])
    workspace.write_season("SP1", 2023, [full_row("Sevilla", "Valencia") | {"Div": "SP1"}])

    assert workspace.run("E0,E1,SP1", "2023") == 1  # only E0 failed

    assert workspace.counts() == {
        "leagues": 1,
        "teams": 2,
        "fixtures": 1,
        "odds_snapshots": ODDS_PER_FULL_ROW,
    }
    assert workspace.run("E1", "2023") == 0


@pytest.mark.slow
def test_ten_thousand_rows_import_within_budget(workspace: ImportWorkspace) -> None:
    # 10k distinct home/away pairings of 101 teams (a pairing is one fixture per season),
    # 50 matches a day from 1 Aug 2023 so every date lies inside the 2023/24 season.
    pairings = [(home, away) for home in range(101) for away in range(101) if home != away]
    rows = [
        full_row(
            f"Team {home}",
            f"Team {away}",
            when=(date(2023, 8, 1) + timedelta(days=i // 50)).strftime("%d/%m/%Y 15:00"),
        )
        for i, (home, away) in enumerate(pairings[:10_000])
    ]
    workspace.write_season("E0", 2023, rows)

    started = time.perf_counter()
    assert workspace.run("E0", "2023") == 0
    elapsed = time.perf_counter() - started

    counts = workspace.counts()
    assert counts["fixtures"] == 10_000
    assert counts["odds_snapshots"] == 10_000 * ODDS_PER_FULL_ROW
    # ~26 seasons in one file (a real one has 306-552 rows); measured ~10 s on the dev box.
    # Generous ceiling: this guards against pathological (e.g. quadratic) regressions, not
    # micro-performance. Coverage tracing and parallel load on CI/dev machines can make the
    # same import 3-4x slower than its ~20 s untraced baseline.
    assert elapsed < IMPORT_10K_CEILING_SECONDS, f"10k-row import took {elapsed:.1f} s"


# ----------------------------------------------------------------------------- network


def test_offline_mode_never_opens_a_connection(workspace: ImportWorkspace) -> None:
    current = workspace.script.current_season(datetime.now(UTC).date())
    row = full_row("Arsenal", "Forest", when=f"12/08/{current} 15:00")  # inside that season
    stale = workspace.write_season("E0", current, [row])
    week_ago = time.time() - 7 * 24 * 3600
    os.utime(stale, (week_ago, week_ago))

    with respx.mock(assert_all_called=False) as router:
        anything = router.route().mock(return_value=httpx.Response(200, content=b"Date,x\n"))
        exit_code = workspace.run("E0,E1", f"{current - 1}-{current}")

    assert exit_code == 0
    assert anything.call_count == 0
    assert workspace.counts()["fixtures"] == 1  # the stale current-season file is still used


BLOCK_PAGE = (
    b"<!DOCTYPE html><html><head><title>Access Denied</title></head><body>"
    b"Category: Gambling. Date, time and URL of this request were logged.</body></html>"
)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403, content=BLOCK_PAGE, headers={"Content-Type": "text/html"}),
        httpx.Response(200, content=BLOCK_PAGE, headers={"Content-Type": "text/csv"}),
        httpx.Response(200, content=b"\xef\xbb\xbf  \n<html>Date, blocked</html>"),
    ],
    ids=["403-block-page", "200-html-as-csv", "bom-and-blank-line-html"],
)
def test_blocked_download_fails_and_is_never_cached(
    workspace: ImportWorkspace, response: httpx.Response
) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(SEASON_2023_E0_URL).mock(return_value=response)
        exit_code = workspace.run("E0", "2023", offline=False)

    assert exit_code == 1
    assert list(workspace.cache_dir.rglob("*")) == []  # neither the file nor a .partial
    assert workspace.counts()["fixtures"] == 0


def test_redirect_to_a_block_page_is_not_cached(workspace: ImportWorkspace) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(SEASON_2023_E0_URL).mock(
            return_value=httpx.Response(302, headers={"Location": "https://filter.corp/blocked"})
        )
        router.get("https://filter.corp/blocked").mock(
            return_value=httpx.Response(200, content=BLOCK_PAGE)
        )
        exit_code = workspace.run("E0", "2023", offline=False)

    assert exit_code == 1
    assert list(workspace.cache_dir.rglob("*")) == []


def test_plain_text_block_page_is_not_cached(workspace: ImportWorkspace) -> None:
    page = b"Access denied by policy (Gambling). Date: Tue, 21 Oct 2026 10:00:00 GMT\n"
    with respx.mock(assert_all_called=False) as router:
        router.get(SEASON_2023_E0_URL).mock(return_value=httpx.Response(200, content=page))
        workspace.run("E0", "2023", offline=False)

    assert not workspace.season_file("E0", 2023).exists()


# ----------------------------------------------------------------------------- unique keys


def insert_signal(
    connection: sqlite3.Connection, market: str, selection: str, line: object
) -> None:
    connection.execute(
        "INSERT INTO signals (user_id, fixture_id, market, selection, line, odds, p_model, ev,"
        " confidence, stake_units, source, status, created_at, updated_at)"
        " VALUES (1, (SELECT min(id) FROM fixtures), ?, ?, ?, 2.0, 0.55, 0.1, 'high', '1.0000',"
        " 'auto', 'pending', '2026-10-21 00:00:00', '2026-10-21 00:00:00')",
        (market, selection, line),
    )


def test_market_line_is_never_null_so_unique_keys_hold(committed: ImportWorkspace) -> None:
    import_committed(committed)
    with sqlite_connection(committed.db) as connection:
        connection.execute(
            "INSERT INTO users (id, is_admin, timezone, created_at)"
            " VALUES (1, 1, 'Asia/Tashkent', '2026-10-21 00:00:00')"
        )
        # The migrated DDL itself (not only the ORM) refuses NULL, which would defeat UNIQUE.
        with pytest.raises(sqlite3.IntegrityError, match=re.escape("NOT NULL constraint failed")):
            insert_signal(connection, "1x2", "home", None)
        insert_signal(connection, "1x2", "home", 0.0)
        for duplicate_line in (0.0, -0.0, 0):
            with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
                insert_signal(connection, "1x2", "home", duplicate_line)
        insert_signal(connection, "asian_handicap", "home", -0.25)
        insert_signal(connection, "asian_handicap", "home", 0.25)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            insert_signal(connection, "1x2", "away", 0.5)
        with pytest.raises(sqlite3.IntegrityError, match=re.escape("odds_snapshots.line")):
            connection.execute(
                "INSERT INTO odds_snapshots (fixture_id, bookmaker, market, selection, line, odds,"
                " kind, taken_at) VALUES ((SELECT min(id) FROM fixtures), 'pinnacle', '1x2',"
                " 'home', NULL, 2.0, 'closing', '2024-01-01 00:00:00')"
            )
