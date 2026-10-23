"""T6 acceptance drills on a database that has really lived a week (not a synthetic fixture).

Backup -> restore -> same numbers; Alembic down two revisions and up again with data in every
table; the CLI health states. The week itself comes from the T6 harness (real services,
scripted provider, hand-checked elsewhere).
"""

import asyncio
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command

from app.db.engine import create_engine, create_session_factory
from app.db.schema import Base
from app.ops.sqlite_tool import (
    alembic_revision,
    backup_to_file,
    check_integrity,
    restore,
    verify,
)
from app.pipeline.context import PipelineContext
from app.pipeline.ledger import Ledger
from app.services.interfaces import ReportRequest
from app.services.real import create_real_services
from tests.db.test_migrations import alembic_config
from tests.e2e.t6_world import USER, Week, open_week, play

pytestmark = pytest.mark.slow

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
async def lived(history_template, tmp_path, bot, session):  # type: ignore[no-untyped-def]
    """A week-old installation: ``(week, database path)`` with bets and settlements in it."""
    async with open_week(history_template, tmp_path, bot, session) as week:
        await play(week)
        yield week, tmp_path / "week.db"


def counts(path: Path) -> dict[str, int]:
    with closing(sqlite3.connect(path)) as db:
        tables = [
            name
            for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if name != "alembic_version" and not name.startswith("apscheduler")
        ]
        return {t: db.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}  # noqa: S608


async def test_a_backup_taken_while_the_bot_runs_restores_to_the_same_numbers(
    lived: tuple[Week, Path], tmp_path: Path
) -> None:
    week, database = lived
    bank = await week.services.bets.bank_summary(USER)
    report = await week.services.reports.report(USER, ReportRequest.all_time())
    export = await week.services.reports.export(USER)
    assert bank.totals.settled > 0
    assert report.settled > 0

    # online backup of the live (WAL, connections open) database, then a verified restore
    backup_file = tmp_path / "backups" / "daily.db.gz"
    backup_to_file(database, backup_file)
    with backup_file.open("rb") as handle:
        assert verify(handle).startswith("integrity_check=ok")
    restored = tmp_path / "restored" / "app.db"
    with backup_file.open("rb") as handle:
        restore(restored, handle)
    check_integrity(restored)
    assert counts(restored) == counts(database)

    engine = create_engine(restored)
    try:
        sessions = create_session_factory(engine)
        ctx = PipelineContext(sessions=sessions, ledger=Ledger(sessions), clock=week.clock)
        services = create_real_services(ctx).services
        assert await services.bets.bank_summary(USER) == bank
        assert await services.reports.report(USER, ReportRequest.all_time()) == report
        again = await services.reports.export(USER)
        assert again.content == export.content  # /eksport is byte-identical after the drill
    finally:
        await engine.dispose()


async def test_restoring_over_a_damaged_database_keeps_a_safety_copy(
    lived: tuple[Week, Path], tmp_path: Path
) -> None:
    _, database = lived
    backup_file = tmp_path / "good.db.gz"
    backup_to_file(database, backup_file)
    broken = tmp_path / "broken" / "app.db"
    broken.parent.mkdir()
    broken.write_bytes(b"this is not a database" * 100)
    with backup_file.open("rb") as handle:
        safety = restore(broken, handle)
    assert safety is not None
    assert safety.read_bytes().startswith(b"this is not a database")
    check_integrity(broken)
    assert counts(broken) == counts(database)


def test_a_truncated_backup_is_refused_and_leaves_the_database_alone(
    lived: tuple[Week, Path], tmp_path: Path
) -> None:
    _, database = lived
    good = tmp_path / "good.db.gz"
    backup_to_file(database, good)
    cut = tmp_path / "cut.db.gz"
    cut.write_bytes(good.read_bytes()[: good.stat().st_size // 2])
    target = tmp_path / "target" / "app.db"
    target.parent.mkdir()
    shutil.copy(database, target)
    before = target.read_bytes()
    with cut.open("rb") as handle, pytest.raises(Exception) as caught:
        restore(target, handle)
    assert caught.value is not None
    assert target.read_bytes() == before


async def test_two_revisions_down_and_up_again_loses_no_signal_bet_or_result(
    lived: tuple[Week, Path], tmp_path: Path
) -> None:
    _, database = lived
    migrated = tmp_path / "migrate" / "app.db"
    migrated.parent.mkdir()
    backup_to_file(database, tmp_path / "pre.db.gz")
    with (tmp_path / "pre.db.gz").open("rb") as handle:
        restore(migrated, handle)
    config = alembic_config(migrated)
    # Alembic's env.py runs its own event loop: keep it off this test's loop
    await asyncio.to_thread(command.stamp, config, "head")  # the harness builds from the models
    before = counts(migrated)
    assert before["signals"] > 0
    assert before["user_bets"] > 0
    assert before["results"] > 0

    await asyncio.to_thread(command.downgrade, config, "0006")
    assert alembic_revision(migrated) == "0006"
    await asyncio.to_thread(command.upgrade, config, "head")

    after = counts(migrated)
    for table in ("fixtures", "odds_snapshots", "signals", "signal_versions", "results"):
        assert after[table] == before[table], table
    assert after["user_bets"] == before["user_bets"]
    assert after["users"] == before["users"]
    await asyncio.to_thread(command.check, config)  # no drift between migrations and models
    check_integrity(migrated)
    with closing(sqlite3.connect(migrated)) as db:
        profit = db.execute("SELECT sum(profit) FROM user_bets").fetchone()[0]
    with closing(sqlite3.connect(database)) as db:
        assert profit == db.execute("SELECT sum(profit) FROM user_bets").fetchone()[0]


def test_every_table_of_the_models_is_in_the_lived_database(lived: tuple[Week, Path]) -> None:
    _, database = lived
    assert set(Base.metadata.tables) <= set(counts(database))


def run_health(
    db_path: Path, *extra: str, services: str = "fake", workdir: Path
) -> tuple[int, str]:
    env = {
        **os.environ,  # the suite fixture already removed every app setting from it
        "DB_PATH": str(db_path),
        "APP_SERVICES": services,
        "TEMP": str(workdir),  # the config-error marker lives in the temp dir
        "TMP": str(workdir),
        "TMPDIR": str(workdir),
    }
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-I", "-m", "app.ops.healthcheck", *extra],
        capture_output=True,
        text=True,
        env=env,
        cwd=workdir,
        timeout=60,
        check=False,
    )
    return done.returncode, done.stdout


def test_the_health_check_states_on_a_lived_database(
    lived: tuple[Week, Path], tmp_path: Path
) -> None:
    _, database = lived
    code, out = run_health(database, workdir=tmp_path)
    assert code == 0, out
    assert '"healthy": true' in out
    assert '"settled"' in out  # last job runs are reported

    missing, out = run_health(tmp_path / "nope" / "app.db", workdir=tmp_path)
    assert missing == 1
    assert "does not exist" in out

    low_disk, out = run_health(database, "--min-free-mb", str(10**9), workdir=tmp_path)
    assert low_disk == 1
    assert '"name": "disk"' in out

    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"x" * 4096)
    code, out = run_health(garbage, workdir=tmp_path)
    assert code == 1
    assert "database" in out

    # real mode expects the scheduler's job store: this database was never scheduled
    code, out = run_health(database, services="real", workdir=tmp_path)
    assert code == 1
    assert '"name": "scheduler", "ok": false' in out
