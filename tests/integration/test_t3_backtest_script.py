"""T3 QA: ``scripts/backtest.py`` end to end (real Alembic database, real importer, real CLI).

No network: history comes from the committed football-data fixture CSVs imported by
``scripts/import_history.py --offline`` or from the synthetic generator.
"""

import asyncio
import importlib.util
import re
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

from app.db.engine import create_engine, create_session_factory
from app.db.history import upsert_batch
from app.domain import HistoryBatch
from app.models.backtest import SyntheticConfig, dataset_fingerprint, generate_history
from tests.integration.history_helpers import FOOTBALL_DATA_FIXTURES, REPO_ROOT, ImportWorkspace

TINY_RUN: Final = [
    *("--xis", "0.0065", "--weights", "0.5", "--min-evs", "0.0", "--min-probs", "0.3"),
    *("--refit-days", "28", "--resamples", "50"),
]
NO_HISTORY: Final = "no history in DB - run scripts/import_history.py first"


@pytest.fixture(scope="module")
def backtest() -> ModuleType:
    """``scripts/backtest.py`` loaded as a module (it is not part of the package)."""
    spec = importlib.util.spec_from_file_location(
        "qa_backtest", REPO_ROOT / "scripts" / "backtest.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _store(db_path: Path, batch: HistoryBatch) -> None:
    engine = create_engine(db_path)
    try:
        async with create_session_factory(engine)() as session, session.begin():
            await upsert_batch(session, batch)
    finally:
        await engine.dispose()


def test_cli_process_on_an_empty_migrated_database_exits_2_with_a_clear_message(
    migrated_db: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    completed = subprocess.run(  # noqa: S603 - fixed argv built from test paths
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "backtest.py"),
            *("--db-path", str(migrated_db), "--out-dir", str(out)),
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 2, completed.stderr
    assert NO_HISTORY in completed.stdout + completed.stderr
    assert not out.exists()


def test_backtest_after_offline_import_of_committed_fixtures(
    backtest: ModuleType,
    workspace: ImportWorkspace,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    shutil.copytree(FOOTBALL_DATA_FIXTURES, workspace.cache_dir, dirs_exist_ok=True)
    assert workspace.run("E0,SP1,D1", "2021-2024") == 0
    assert workspace.counts()["fixtures"] == 9
    capsys.readouterr()

    out = tmp_path / "out"
    code = backtest.main(["--db-path", str(workspace.db), "--out-dir", str(out), *TINY_RUN])

    # Three seasons exist (SP1 2021, D1 2023, E0 2024) but each league has a single season,
    # so no league has any history before its validation fixtures: nothing can be scored.
    # The correct outcome is a clean refusal, not a report built on nothing. Since the F-1
    # fix the refusal comes earlier: each fixture file holds only a few matches, far from a
    # full double round robin, so no season counts as completed for the hold-out.
    assert code == 2
    assert "no completed season" in capsys.readouterr().out
    assert not out.exists() or not any(out.iterdir())


def test_synthetic_report_is_reproducible_and_records_seed_hash_and_commit(
    backtest: ModuleType, tmp_path: Path
) -> None:
    seed = 4242
    args = [
        "--synthetic",
        *("--synthetic-leagues", "1", "--synthetic-teams", "6", "--synthetic-seasons", "3"),
        *("--seed", str(seed), *TINY_RUN),
    ]
    reports = []
    for run in ("a", "b"):
        out = tmp_path / run
        assert backtest.main([*args, "--out-dir", str(out)]) == 0
        (report,) = out.glob("*-synthetic.md")
        reports.append(report.read_text(encoding="utf-8"))

    expected_hash = dataset_fingerprint(
        generate_history(SyntheticConfig(leagues=1, teams_per_league=6, seasons=3, seed=seed))
    )
    for text in reports:
        assert re.search(r"\| Git commit \| `[0-9a-f]{40}(-dirty)?` \|", text), text[:2000]
        assert f"| Ma'lumot izi (SHA-256) | `{expected_hash}` |" in text
        assert f"| Seed (bootstrap) | `{seed}` |" in text
        assert f"| synthetic.seed | `{seed}` |" in text

    def without_timestamp(text: str) -> str:
        return re.sub(r"^- \*\*Sana:\*\* .*$", "", text, flags=re.MULTILINE)

    # Same seed and data: every number in the report is identical (only the time differs).
    assert without_timestamp(reports[0]) == without_timestamp(reports[1])


def test_a_season_still_in_progress_is_not_the_default_holdout(
    backtest: ModuleType, migrated_db: Path, tmp_path: Path
) -> None:
    batch = generate_history(
        SyntheticConfig(leagues=1, teams_per_league=6, seasons=4, first_season=2023, seed=5)
    )
    # Season 2026 has started: only its first rounds are played (and therefore in the DB),
    # exactly what an import of the current football-data file produces.
    current = [f for f in batch.fixtures if f.season == 2026]
    unplayed = {f.ref for f in current[len(current) // 3 :]}
    in_progress = replace(
        batch,
        fixtures=tuple(f for f in batch.fixtures if f.ref not in unplayed),
        results=tuple(r for r in batch.results if r.fixture not in unplayed),
        odds=tuple(o for o in batch.odds if o.fixture not in unplayed),
    )
    asyncio.run(_store(migrated_db, in_progress))
    out = tmp_path / "out"
    code = backtest.main(
        ["--db-path", str(migrated_db), "--out-dir", str(out), "--label", "db", *TINY_RUN]
    )
    assert code == 0
    (report,) = out.glob("*-db.md")
    assert "hold-out 2025" in report.read_text(encoding="utf-8")
