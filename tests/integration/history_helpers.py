"""Helpers for the history-import QA tests: CSV builders and a raw SQLite oracle."""

import csv
import io
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
FOOTBALL_DATA_FIXTURES: Final = REPO_ROOT / "tests" / "fixtures" / "football_data"

_IDENTITY_COLUMNS: Final = (
    "Div",
    "Date",
    "Time",
    "HomeTeam",
    "AwayTeam",
    "FTHG",
    "FTAG",
    "FTR",
    "HTHG",
    "HTAG",
    "HTR",
)
_PRICE_PREFIXES: Final = ("B365", "PS", "Max", "Avg")
_TOTALS_PREFIXES: Final = ("B365", "P", "Max", "Avg")


def _odds_columns(*, at_close: bool) -> list[str]:
    """Price columns of one moment, spelled as in current football-data files."""
    c = "C" if at_close else ""
    columns = [f"{p}{c}{s}" for p in _PRICE_PREFIXES for s in "HDA"]
    columns += [f"{p}{c}{side}2.5" for p in _TOTALS_PREFIXES for side in "><"]
    columns.append("AHCh" if at_close else "AHh")
    columns += [f"{p}{c}AH{s}" for p in ("B365", "P", "Max", "Avg") for s in "HA"]
    return columns


# Header of a current football-data file with every odds group the importer reads.
FULL_HEADER: Final = (
    *_IDENTITY_COLUMNS,
    *_odds_columns(at_close=False),
    *_odds_columns(at_close=True),
)
LINE_COLUMNS: Final = ("AHh", "AHCh")
PRICE_COLUMNS: Final = tuple(
    name for name in FULL_HEADER if name not in _IDENTITY_COLUMNS and name not in LINE_COLUMNS
)
# One fully quoted row yields 8 x 1X2 (3) + 8 x totals (2) + 8 x handicap (2) prices.
ODDS_PER_FULL_ROW: Final = 8 * 3 + 8 * 2 + 8 * 2


def full_row(
    home: str,
    away: str,
    *,
    when: str = "12/08/2023 15:00",
    score: tuple[int, int] = (1, 0),
    line: str = "-0.5",
) -> dict[str, str]:
    """A fully quoted E0 row (``when`` = ``"dd/mm/yyyy[ HH:MM]"``); every price is 1.90."""
    date, _, time = when.partition(" ")
    row = {
        "Div": "E0",
        "Date": date,
        "Time": time,
        "HomeTeam": home,
        "AwayTeam": away,
        "FTHG": str(score[0]),
        "FTAG": str(score[1]),
        "HTHG": "0",
        "HTAG": "0",
    }
    row.update(dict.fromkeys(PRICE_COLUMNS, "1.90"))
    row.update(dict.fromkeys(LINE_COLUMNS, line))
    return row


def write_csv(
    path: Path, rows: Sequence[Mapping[str, str]], header: Sequence[str] = FULL_HEADER
) -> Path:
    """Write a football-data style CSV (cells a row does not set are left empty)."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(header), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({name: row.get(name, "") for name in header})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(buffer.getvalue(), encoding="utf-8")
    return path


@contextmanager
def sqlite_connection(path: Path) -> Iterator[sqlite3.Connection]:
    """Plain sqlite3 connection: an oracle independent of the ORM and the mappers."""
    with closing(sqlite3.connect(path)) as connection, connection:
        yield connection


@dataclass(frozen=True, slots=True)
class ImportWorkspace:
    """The importer script, a migrated database and a cache directory of one test."""

    script: ModuleType
    db: Path
    cache_dir: Path

    def season_file(self, league: str, season: int) -> Path:
        """Cache location of one league season (spelled out independently of the code)."""
        return self.cache_dir / f"{season % 100:02d}{(season + 1) % 100:02d}" / f"{league}.csv"

    def write_season(self, league: str, season: int, rows: Sequence[Mapping[str, str]]) -> Path:
        """Put a season file into the cache."""
        return write_csv(self.season_file(league, season), rows)

    def run(self, leagues: str, seasons: str, *, offline: bool = True) -> int:
        """Run the importer's real ``main`` and return its exit code."""
        argv = ["--leagues", leagues, "--seasons", seasons, "--cache-dir", str(self.cache_dir)]
        argv += ["--db-path", str(self.db), *(["--offline"] if offline else [])]
        exit_code: int = self.script.main(argv)
        return exit_code

    def query(self, sql: str, *params: object) -> list[tuple[object, ...]]:
        """All rows of ``sql``."""
        with sqlite_connection(self.db) as connection:
            return [tuple(row) for row in connection.execute(sql, params)]

    def counts(self) -> dict[str, int]:
        """Row counts of the tables the importer writes."""
        tables = ("leagues", "teams", "fixtures", "odds_snapshots")
        return {
            table: int(self.query(f"SELECT count(*) FROM {table}")[0][0])  # noqa: S608
            for table in tables
        }

    def fixture(self, external_id_suffix: str) -> tuple[object, ...]:
        """``(kickoff_at, season, status, home, away, home HT, away HT)`` of one fixture."""
        rows = self.query(
            "SELECT kickoff_at, season, status, home_score, away_score, home_ht_score,"
            " away_ht_score FROM fixtures WHERE external_id LIKE ?",
            f"%{external_id_suffix}",
        )
        assert len(rows) == 1, (external_id_suffix, rows)
        return rows[0]

    def prices(
        self, external_id_suffix: str, *, bookmaker: str, market: str, kind: str
    ) -> set[tuple[object, ...]]:
        """``(selection, line, odds)`` of one price group of one fixture."""
        return set(
            self.query(
                "SELECT o.selection, o.line, o.odds FROM odds_snapshots o"
                " JOIN fixtures f ON f.id = o.fixture_id WHERE f.external_id LIKE ?"
                " AND o.bookmaker = ? AND o.market = ? AND o.kind = ?",
                f"%{external_id_suffix}",
                bookmaker,
                market,
                kind,
            )
        )

    def grouped(self, sql: str) -> dict[object, int]:
        """``{key: count}`` of a GROUP BY query; a multi-column key becomes a tuple."""
        return {
            (row[0] if len(row) == 2 else row[:-1]): int(str(row[-1])) for row in self.query(sql)
        }
