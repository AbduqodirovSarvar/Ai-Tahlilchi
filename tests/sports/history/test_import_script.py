"""End-to-end tests of scripts/import_history.py on the committed fixture CSVs (no network)."""

import argparse
import importlib.util
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from types import ModuleType

import httpx
import pytest
import respx
from alembic import command
from alembic.config import Config

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "football_data"
# Fixture files: E0 2024/25 (4 valid rows), SP1 2021/22 (3), D1 2023/24 (2).
EXPECTED_FIXTURES = 4 + 3 + 2
EXPECTED_ODDS = 165 + 27 + 6
ALL_FIXTURE_FILES = ["--leagues", "E0,SP1,D1", "--seasons", "2021-2024"]


@pytest.fixture(scope="module")
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "import_history", REPO_ROOT / "scripts" / "import_history.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "app.db"
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.attributes["db_path"] = path
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")
    return path


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return Path(shutil.copytree(FIXTURES_DIR, tmp_path / "history"))


def counts(db_path: Path) -> tuple[int, ...]:
    with closing(sqlite3.connect(db_path)) as connection:
        return tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608
            for table in ("fixtures", "odds_snapshots", "teams")
        )


def run(script: ModuleType, db_path: Path, cache_dir: Path, *extra: str) -> int:
    exit_code: int = script.main(
        [*ALL_FIXTURE_FILES, "--cache-dir", str(cache_dir), "--db-path", str(db_path), *extra]
    )
    return exit_code


def test_offline_import_is_idempotent(
    script: ModuleType, db_path: Path, cache_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(script, db_path, cache_dir, "--offline") == 0
    first = counts(db_path)
    assert first[:2] == (EXPECTED_FIXTURES, EXPECTED_ODDS)

    assert run(script, db_path, cache_dir, "--offline") == 0
    assert counts(db_path) == first
    output = capsys.readouterr().out
    assert "season_imported" in output
    assert "coverage_total" in output
    assert "season_file_unavailable" in output  # e.g. E0 2021/22 is not in the fixtures


def test_missing_schema_is_a_configuration_error(
    script: ModuleType, tmp_path: Path, cache_dir: Path
) -> None:
    assert run(script, tmp_path / "empty.db", cache_dir, "--offline") == 2


def test_broken_file_fails_alone(script: ModuleType, db_path: Path, cache_dir: Path) -> None:
    (cache_dir / "2425" / "E0.csv").write_bytes(b"<html>blocked</html>")
    assert run(script, db_path, cache_dir, "--offline") == 1
    assert counts(db_path)[0] == 3 + 2  # SP1 and D1 were still imported


@respx.mock
def test_downloads_missing_files_into_the_cache(
    script: ModuleType, db_path: Path, tmp_path: Path
) -> None:
    csv = (FIXTURES_DIR / "2425" / "E0.csv").read_bytes()
    respx.get("https://www.football-data.co.uk/mmz4281/2425/E0.csv").mock(
        return_value=httpx.Response(200, content=csv)
    )
    respx.get().mock(return_value=httpx.Response(404))
    cache_dir = tmp_path / "fresh-cache"

    exit_code = script.main(
        [
            "--leagues",
            "E0",
            "--seasons",
            "2023-2024",
            "--cache-dir",
            str(cache_dir),
            "--db-path",
            str(db_path),
        ]
    )

    assert exit_code == 0
    assert (cache_dir / "2425" / "E0.csv").read_bytes() == csv
    assert counts(db_path)[0] == 4


@pytest.mark.parametrize(
    ("text", "expected"), [("2021-2023", (2021, 2022, 2023)), ("2023,2025", (2023, 2025))]
)
def test_parse_seasons(script: ModuleType, text: str, expected: tuple[int, ...]) -> None:
    assert script.parse_seasons(text) == expected


@pytest.mark.parametrize("text", ["abc", "2021-x"])
def test_parse_seasons_rejects_garbage(script: ModuleType, text: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        script.parse_seasons(text)


def test_parse_leagues(script: ModuleType) -> None:
    assert script.parse_leagues(" e0, sp1 ") == ("E0", "SP1")
    for text in ("XX", ""):
        with pytest.raises(argparse.ArgumentTypeError):
            script.parse_leagues(text)


def test_defaults_are_the_agreed_leagues_and_seasons(script: ModuleType) -> None:
    options = script.parse_args([], today_season=2026, default_db=Path("data/app.db"))
    assert options.leagues == ("E0", "E1", "SP1", "D1", "I1", "F1", "N1", "P1", "B1", "T1")
    assert options.seasons == (2021, 2022, 2023, 2024, 2025, 2026)
    assert options.cache_dir == Path("data/history")
    assert not options.offline
