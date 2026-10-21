"""End-to-end tests of scripts/backtest.py (tiny data, temporary database and output)."""

import asyncio
import importlib.util
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest
from alembic import command
from alembic.config import Config

from app.db.engine import create_engine, create_session_factory
from app.db.history import upsert_batch
from app.domain import HistoryBatch
from app.models.backtest import SyntheticConfig, generate_history

REPO_ROOT = Path(__file__).resolve().parents[2]
TINY_GRID = ["--xis", "0.0065", "--weights", "0.5", "--min-evs", "0.0", "--min-probs", "0.3"]
TINY_RUN = [*TINY_GRID, "--refit-days", "28", "--resamples", "50"]


@pytest.fixture(scope="module")
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "backtest_script", REPO_ROOT / "scripts" / "backtest.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migrated_db(tmp_path: Path) -> Path:
    path = tmp_path / "app.db"
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.attributes["db_path"] = path
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")
    return path


def test_missing_database_exits_cleanly(
    script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nope.db"
    assert script.main(["--db-path", str(missing), "--out-dir", str(tmp_path)]) == 2
    assert "no history in DB - run scripts/import_history.py first" in capsys.readouterr().out
    assert not missing.exists()  # the script must not create an empty database


def test_empty_database_exits_cleanly(
    script: ModuleType, migrated_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert script.main(["--db-path", str(migrated_db), "--out-dir", str(tmp_path / "out")]) == 2
    assert "no history in DB" in capsys.readouterr().out
    assert not (tmp_path / "out").exists()


def test_database_without_schema_exits_cleanly(script: ModuleType, tmp_path: Path) -> None:
    bare = tmp_path / "bare.db"
    bare.write_bytes(b"")
    assert script.main(["--db-path", str(bare), "--out-dir", str(tmp_path)]) == 2


async def store(db_path: Path, batch: HistoryBatch) -> None:
    engine = create_engine(db_path)
    try:
        async with create_session_factory(engine)() as session, session.begin():
            await upsert_batch(session, batch)
    finally:
        await engine.dispose()


def tiny_batch(seasons: int) -> HistoryBatch:
    return generate_history(
        SyntheticConfig(leagues=1, teams_per_league=6, seasons=seasons, first_season=2020, seed=5)
    )


def partly_played(batch: HistoryBatch, season: int) -> HistoryBatch:
    """What importing a current football-data file gives: only the first third is played."""
    current = [f for f in batch.fixtures if f.season == season]
    unplayed = {f.ref for f in current[len(current) // 3 :]}
    return replace(
        batch,
        fixtures=tuple(f for f in batch.fixtures if f.ref not in unplayed),
        results=tuple(r for r in batch.results if r.fixture not in unplayed),
        odds=tuple(o for o in batch.odds if o.fixture not in unplayed),
    )


MID_SEASON: Final = date(2023, 11, 20)  # 2023/24 is being played


def run_db(script: ModuleType, db: Path, out: Path, *extra: str, today: date = MID_SEASON) -> int:
    args = ["--db-path", str(db), "--out-dir", str(out), "--label", "db", *TINY_RUN, *extra]
    code: int = script.main(args, today=today)
    return code


def test_real_mode_skips_the_season_in_progress_as_holdout(
    script: ModuleType, migrated_db: Path, tmp_path: Path
) -> None:
    # 2020..2023 stored; 2023/24 is in progress (all stored fixtures FINISHED, like football-data).
    asyncio.run(store(migrated_db, partly_played(tiny_batch(seasons=4), 2023)))
    out = tmp_path / "out"
    assert run_db(script, migrated_db, out) == 0
    (report,) = out.glob("*-db.md")
    text = report.read_text(encoding="utf-8")
    assert "haqiqiy (DB)" in text
    assert "hold-out 2022" in text
    assert "| incomplete seasons (never hold-out) | `[2023]` |" in text
    assert len(list(out.glob("*-db-*.png"))) == 2


def test_a_partly_imported_past_season_is_not_complete_either(
    script: ModuleType, migrated_db: Path, tmp_path: Path
) -> None:
    # Long after the season ended, but only a third of 2023 was imported: the count rule.
    asyncio.run(store(migrated_db, partly_played(tiny_batch(seasons=4), 2023)))
    out = tmp_path / "out"
    assert run_db(script, migrated_db, out, today=date(2030, 1, 1)) == 0
    (report,) = out.glob("*-db.md")
    assert "hold-out 2022" in report.read_text(encoding="utf-8")


def test_explicit_holdout_must_be_a_completed_season(
    script: ModuleType, migrated_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    asyncio.run(store(migrated_db, partly_played(tiny_batch(seasons=4), 2023)))
    assert run_db(script, migrated_db, tmp_path, "--holdout-season", "2023") == 2
    assert "--holdout-season 2023 is not a completed season" in capsys.readouterr().out
    out = tmp_path / "explicit"
    assert run_db(script, migrated_db, out, "--holdout-season", "2022") == 0


def test_no_completed_season_exits_cleanly(
    script: ModuleType, migrated_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    asyncio.run(store(migrated_db, tiny_batch(seasons=3)))  # 2020..2022
    assert run_db(script, migrated_db, tmp_path, today=date(2021, 3, 1)) == 2
    assert "no completed season" in capsys.readouterr().out


def test_real_mode_with_too_few_seasons_exits_cleanly(
    script: ModuleType, migrated_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    asyncio.run(store(migrated_db, tiny_batch(seasons=2)))
    assert run_db(script, migrated_db, tmp_path) == 2
    assert "need >= 3 seasons" in capsys.readouterr().out


def test_bad_synthetic_configuration_exits_cleanly(
    script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["--synthetic", "--synthetic-teams", "5", "--out-dir", str(tmp_path)]
    assert script.main(args) == 2
    assert "teams_per_league must be even" in capsys.readouterr().out


def test_synthetic_mode_writes_report_with_sanity_note(script: ModuleType, tmp_path: Path) -> None:
    args = [
        "--synthetic",
        "--synthetic-leagues",
        "1",
        "--synthetic-teams",
        "6",
        "--synthetic-seasons",
        "3",
        "--out-dir",
        str(tmp_path),
        *TINY_RUN,
    ]
    assert script.main(args) == 0
    (report,) = tmp_path.glob("*-synthetic.md")
    text = report.read_text(encoding="utf-8")
    assert "Haqiqiy ma'lumotdagi F3" in text
    assert "Sanity tekshiruvi" in text
    assert "synthetic.teams_per_league | `6`" in text
    assert "## F3 darvozasi (hold-out, qaror #11)" in text


def test_pure_market_choice_is_explained(script: ModuleType, tmp_path: Path) -> None:
    tiny = ["--synthetic-leagues", "1", "--synthetic-teams", "6", "--synthetic-seasons", "3"]
    run = ["--xis", "0.0065", "--weights", "0", "--min-evs", "0.0", "--min-probs", "0.3"]
    run += ["--refit-days", "28", "--resamples", "50"]
    assert script.main(["--synthetic", *tiny, "--out-dir", str(tmp_path), *run]) == 0
    (report,) = tmp_path.glob("*-synthetic.md")
    text = report.read_text(encoding="utf-8")
    assert "sof bozorni (w = 0) tanladi" in text
    assert "Chegaraviy w = 0" in text


def test_float_list_parsing(script: ModuleType) -> None:
    assert script.float_list("0.1, 0.2") == (0.1, 0.2)
    for bad in ("x", ","):
        with pytest.raises(Exception, match="expected"):
            script.float_list(bad)
