"""End-to-end tests of scripts/backtest.py (tiny data, temporary database and output)."""

import asyncio
import importlib.util
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest
from alembic import command
from alembic.config import Config

from app.db.engine import create_engine, create_session_factory
from app.db.history import upsert_batch
from app.domain import FixtureStatus, HistoryBatch
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


def test_real_mode_reads_the_database_and_writes_the_report(
    script: ModuleType, migrated_db: Path, tmp_path: Path
) -> None:
    batch = tiny_batch(seasons=4)
    # The last season is "in progress": one fixture still scheduled, so it is not a hold-out.
    unplayed = batch.fixtures[-1]
    in_progress = HistoryBatch(
        fixtures=(*batch.fixtures[:-1], replace(unplayed, status=FixtureStatus.SCHEDULED)),
        results=batch.results[:-1],
        odds=batch.odds,
    )
    asyncio.run(store(migrated_db, in_progress))
    out = tmp_path / "out"
    code = script.main(
        ["--db-path", str(migrated_db), "--out-dir", str(out), "--label", "db", *TINY_RUN]
    )
    assert code == 0
    (report,) = out.glob("*-db.md")
    text = report.read_text(encoding="utf-8")
    assert "haqiqiy (DB)" in text
    assert "hold-out 2022" in text  # 2023 has an unplayed fixture
    assert len(list(out.glob("*-db-*.png"))) == 2


def test_real_mode_with_too_few_seasons_exits_cleanly(
    script: ModuleType, migrated_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    asyncio.run(store(migrated_db, tiny_batch(seasons=2)))
    assert script.main(["--db-path", str(migrated_db), "--out-dir", str(tmp_path), *TINY_RUN]) == 2
    assert "need >= 3 seasons" in capsys.readouterr().out


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


def test_float_list_parsing(script: ModuleType) -> None:
    assert script.float_list("0.1, 0.2") == (0.1, 0.2)
    for bad in ("x", ","):
        with pytest.raises(Exception, match="expected"):
            script.float_list(bad)
