"""Backup and restore scripts, including the restore drill (backup -> disaster -> restore).

The scripts normally run the sqlite tool inside the application image; here ``TOOL_CMD``
points at the same module on the host, so the shell logic is tested for real without Docker.
"""

import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from tests.ops.shell import ROOT, posix, requires_bash, run_script, tool_command

BACKUP_SH = ROOT / "scripts" / "ops" / "backup.sh"
RESTORE_SH = ROOT / "scripts" / "ops" / "restore.sh"
ALEMBIC_INI = ROOT / "alembic.ini"

pytestmark = requires_bash


def alembic_config(db_path: Path) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.attributes["db_path"] = db_path
    config.attributes["configure_logger"] = False
    return config


def migrated_database(path: Path) -> Path:
    command.upgrade(alembic_config(path), "head")
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "INSERT INTO leagues (sport, source, external_id, name, created_at, updated_at)"
            " VALUES ('football', 'src', 'E0', 'Premier League', '2024-01-01', '2024-01-01')"
        )
        db.commit()
    return path


def environment(tmp_path: Path, database: Path, **extra: str) -> dict[str, str]:
    env = {
        "APP_DIR": posix(tmp_path),
        "BACKUP_DIR": posix(tmp_path / "backups"),
        "DB_PATH": posix(database),
        "TOOL_CMD": tool_command(),
        "STOP_CMD": "true",
        "START_CMD": "true",
    }
    env.update(extra)
    return env


def leagues(path: Path) -> list[tuple[str, str]]:
    with closing(sqlite3.connect(path)) as db:
        return db.execute("SELECT external_id, name FROM leagues").fetchall()


def test_backup_creates_a_verified_archive(tmp_path: Path) -> None:
    database = migrated_database(tmp_path / "app.db")

    result = run_script(BACKUP_SH, env=environment(tmp_path, database))

    assert result.returncode == 0, result.stderr
    archives = list((tmp_path / "backups").glob("app-*-daily.db.gz"))
    assert len(archives) == 1
    assert not list((tmp_path / "backups").glob("*.part"))
    assert archives[0].stat().st_size > 0


def test_restore_drill_recovers_content_and_schema(tmp_path: Path) -> None:
    """The F9.4 acceptance test: a backup that was never restored is not a backup."""
    database = migrated_database(tmp_path / "data" / "app.db")
    env = environment(tmp_path, database)
    assert run_script(BACKUP_SH, env=env).returncode == 0
    archive = next((tmp_path / "backups").glob("app-*.db.gz"))

    # Disaster: the database is replaced by garbage and its WAL files are stale.
    database.write_bytes(b"destroyed" * 500)
    Path(f"{database}-wal").write_bytes(b"stale")

    result = run_script(RESTORE_SH, "--yes", posix(archive), env=env)

    assert result.returncode == 0, result.stderr
    assert leagues(database) == [("E0", "Premier League")]
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    # The restored schema is exactly what the application expects.
    command.check(alembic_config(database))
    # The destroyed file was kept for forensics.
    assert list((database.parent / "pre-restore").glob("app-before-restore-*.db"))


def test_restore_requires_confirmation(tmp_path: Path) -> None:
    database = migrated_database(tmp_path / "app.db")
    env = environment(tmp_path, database)
    run_script(BACKUP_SH, env=env)
    archive = next((tmp_path / "backups").glob("app-*.db.gz"))
    with closing(sqlite3.connect(database)) as db:
        db.execute("DELETE FROM leagues")
        db.commit()

    declined = run_script(RESTORE_SH, posix(archive), env=env, stdin="no\n")
    assert declined.returncode == 65
    assert leagues(database) == []

    confirmed = run_script(RESTORE_SH, posix(archive), env=env, stdin="RESTORE\n")
    assert confirmed.returncode == 0, confirmed.stderr
    assert leagues(database) == [("E0", "Premier League")]


def test_restore_refuses_a_corrupt_backup_before_touching_anything(tmp_path: Path) -> None:
    database = migrated_database(tmp_path / "app.db")
    bad = tmp_path / "bad.db.gz"
    bad.write_bytes(b"not a backup" * 100)

    result = run_script(RESTORE_SH, "--yes", posix(bad), env=environment(tmp_path, database))

    assert result.returncode == 1
    assert "does not verify" in result.stderr
    assert leagues(database) == [("E0", "Premier League")]


def test_restore_dry_run_changes_nothing(tmp_path: Path) -> None:
    database = migrated_database(tmp_path / "app.db")
    env = environment(tmp_path, database)
    run_script(BACKUP_SH, env=env)
    archive = next((tmp_path / "backups").glob("app-*.db.gz"))
    with closing(sqlite3.connect(database)) as db:
        db.execute("DELETE FROM leagues")
        db.commit()

    result = run_script(RESTORE_SH, "--dry-run", posix(archive), env=env)

    assert result.returncode == 0
    assert "[dry-run]" in result.stderr
    assert leagues(database) == []


def test_restore_usage_errors(tmp_path: Path) -> None:
    env = environment(tmp_path, tmp_path / "app.db")
    assert run_script(RESTORE_SH, env=env).returncode == 64
    assert run_script(RESTORE_SH, "--bogus", env=env).returncode == 64
    assert run_script(RESTORE_SH, posix(tmp_path / "missing.gz"), env=env).returncode == 1


def test_backup_of_a_missing_database_fails_without_leaving_files(tmp_path: Path) -> None:
    result = run_script(BACKUP_SH, env=environment(tmp_path, tmp_path / "none.db"))

    assert result.returncode == 1
    assert not list((tmp_path / "backups").glob("app-*"))
    assert not list((tmp_path / "backups").glob("*.part"))


def test_backup_prunes_only_old_archives(tmp_path: Path) -> None:
    database = migrated_database(tmp_path / "app.db")
    backups = tmp_path / "backups"
    backups.mkdir()
    old = backups / "app-20200101T000000Z-daily.db.gz"
    recent = backups / "app-20990101T000000Z-daily.db.gz"
    unrelated = backups / "notes.txt"
    for file in (old, recent, unrelated):
        file.write_bytes(b"x")
    long_ago = time.time() - 30 * 86400
    os.utime(old, (long_ago, long_ago))
    os.utime(unrelated, (long_ago, long_ago))

    result = run_script(BACKUP_SH, env=environment(tmp_path, database, RETENTION_DAYS="14"))

    assert result.returncode == 0, result.stderr
    assert not old.exists()
    assert recent.exists()
    assert unrelated.exists()


def test_backup_dry_run_writes_nothing(tmp_path: Path) -> None:
    database = migrated_database(tmp_path / "app.db")
    result = run_script(BACKUP_SH, "--dry-run", env=environment(tmp_path, database))
    assert result.returncode == 0
    assert "[dry-run]" in result.stderr
    assert not (tmp_path / "backups").exists()


@pytest.mark.parametrize(
    ("args", "env"),
    [(["--bogus"], {}), (["--label", "bad label!"], {}), ([], {"RETENTION_DAYS": "abc"})],
)
def test_backup_rejects_bad_arguments(tmp_path: Path, args: list[str], env: dict[str, str]) -> None:
    result = run_script(BACKUP_SH, *args, env=environment(tmp_path, tmp_path / "app.db", **env))
    assert result.returncode == 64


def test_remote_copy_failure_keeps_the_local_backup_and_exits_3(tmp_path: Path) -> None:
    database = migrated_database(tmp_path / "app.db")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    rclone = fake_bin / "rclone"
    rclone.write_text("#!/usr/bin/env bash\nexit 1\n", newline="\n")
    rclone.chmod(0o755)
    env = environment(tmp_path, database, RCLONE_REMOTE="remote:bucket")
    env["PATH"] = f"{posix(fake_bin)}:{os.environ.get('PATH', '')}"

    result = run_script(BACKUP_SH, env=env)

    assert result.returncode == 3
    assert list((tmp_path / "backups").glob("app-*.db.gz"))
    assert "local backup is kept" in result.stderr


def test_remote_copy_success_uses_rclone_with_retention(tmp_path: Path) -> None:
    database = migrated_database(tmp_path / "app.db")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "rclone.log"
    rclone = fake_bin / "rclone"
    rclone.write_text(f'#!/usr/bin/env bash\necho "$*" >> "{posix(log)}"\n', newline="\n")
    rclone.chmod(0o755)
    env = environment(tmp_path, database, RCLONE_REMOTE="remote:bucket", RETENTION_DAYS="9")
    env["PATH"] = f"{posix(fake_bin)}:{os.environ.get('PATH', '')}"

    result = run_script(BACKUP_SH, env=env)

    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    assert calls[0].startswith("copy ") and calls[0].endswith("remote:bucket --quiet")
    assert "delete remote:bucket --min-age 9d" in calls[1]
