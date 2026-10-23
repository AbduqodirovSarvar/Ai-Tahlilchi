"""Backup, verify and restore: online snapshot, integrity gate, reversible restore."""

import gzip
import io
import sqlite3
import subprocess
import sys
import tempfile
import threading
from contextlib import closing
from pathlib import Path

import pytest

from app.errors import DataError
from app.ops.sqlite_tool import (
    alembic_revision,
    backup,
    backup_to_file,
    check_integrity,
    main,
    restore,
    verify,
)


def make_db(path: Path, rows: int = 3, revision: str | None = "abc123") -> Path:
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
        db.executemany("INSERT INTO items (name) VALUES (?)", [(f"n{i}",) for i in range(rows)])
        if revision:
            db.execute("CREATE TABLE alembic_version (version_num TEXT)")
            db.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
        db.commit()
    return path


def names(path: Path) -> list[str]:
    with closing(sqlite3.connect(path)) as db:
        return [row[0] for row in db.execute("SELECT name FROM items ORDER BY id")]


def backup_bytes(path: Path) -> bytes:
    buffer = io.BytesIO()
    backup(path, buffer)
    return buffer.getvalue()


def test_backup_is_gzip_and_restorable(tmp_path: Path) -> None:
    source = make_db(tmp_path / "app.db", rows=5)
    data = backup_bytes(source)

    assert data[:2] == b"\x1f\x8b"
    target = tmp_path / "restored.db"
    restore(target, io.BytesIO(data))
    assert names(target) == [f"n{i}" for i in range(5)]


def test_backup_never_touches_the_source(tmp_path: Path) -> None:
    source = make_db(tmp_path / "app.db")
    before = names(source)
    backup_bytes(source)
    assert names(source) == before


def test_backup_of_missing_database_fails_cleanly(tmp_path: Path) -> None:
    with pytest.raises(DataError, match="does not exist"):
        backup(tmp_path / "none.db", io.BytesIO())


def test_backup_of_a_corrupt_database_fails_and_leaves_no_file(tmp_path: Path) -> None:
    corrupt = tmp_path / "app.db"
    corrupt.write_bytes(b"not a database at all" * 200)
    destination = tmp_path / "out" / "app.db.gz"
    with pytest.raises(DataError):
        backup_to_file(corrupt, destination)
    assert not destination.exists()
    assert not list((tmp_path / "out").glob("*"))


def test_backup_while_another_connection_writes(tmp_path: Path) -> None:
    """The snapshot is consistent even though a writer is active (the bot keeps running)."""
    source = make_db(tmp_path / "app.db", rows=10)
    stop = threading.Event()

    def writer() -> None:
        with closing(sqlite3.connect(source, timeout=10)) as db:
            while not stop.is_set():
                db.execute("INSERT INTO items (name) VALUES ('w')")
                db.commit()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        data = backup_bytes(source)
    finally:
        stop.set()
        thread.join()
    target = tmp_path / "copy.db"
    restore(target, io.BytesIO(data))
    check_integrity(target)
    assert len(names(target)) >= 10


def test_verify_reports_revision_for_gzip_and_plain(tmp_path: Path) -> None:
    source = make_db(tmp_path / "app.db")
    assert verify(io.BytesIO(backup_bytes(source))) == "integrity_check=ok alembic_revision=abc123"
    with closing(sqlite3.connect(source)) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert "abc123" in verify(io.BytesIO(source.read_bytes()))
    unmigrated = make_db(tmp_path / "u.db", revision=None)
    assert alembic_revision(unmigrated) == "none"


def test_verify_rejects_garbage_and_truncated_gzip(tmp_path: Path) -> None:
    with pytest.raises(DataError):
        verify(io.BytesIO(b"garbage" * 100))
    good = backup_bytes(make_db(tmp_path / "app.db"))
    with pytest.raises((EOFError, OSError)):
        verify(io.BytesIO(good[: len(good) // 2]))


def test_restore_keeps_a_safety_copy_and_drops_stale_wal(tmp_path: Path) -> None:
    live = make_db(tmp_path / "app.db", rows=2)
    older = backup_bytes(make_db(tmp_path / "old.db", rows=7))
    (tmp_path / "app.db-wal").write_bytes(b"stale")
    (tmp_path / "app.db-shm").write_bytes(b"stale")

    safety = restore(live, io.BytesIO(older))

    assert names(live) == [f"n{i}" for i in range(7)]
    assert safety is not None
    assert names(safety) == ["n0", "n1"]
    assert not (tmp_path / "app.db-wal").exists()
    assert not (tmp_path / "app.db-shm").exists()


def test_restore_into_empty_location_has_no_safety_copy(tmp_path: Path) -> None:
    data = backup_bytes(make_db(tmp_path / "src.db"))
    assert restore(tmp_path / "new" / "app.db", io.BytesIO(data)) is None
    assert (tmp_path / "new" / "app.db").is_file()


def test_a_bad_backup_never_replaces_the_live_database(tmp_path: Path) -> None:
    live = make_db(tmp_path / "app.db", rows=4)
    with pytest.raises(DataError):
        restore(live, io.BytesIO(b"definitely not sqlite" * 50))
    assert names(live) == ["n0", "n1", "n2", "n3"]
    assert not list(tmp_path.glob("*.restoring"))


def test_a_truncated_gzip_never_replaces_the_live_database(tmp_path: Path) -> None:
    live = make_db(tmp_path / "app.db", rows=4)
    good = backup_bytes(make_db(tmp_path / "other.db", rows=50))
    with pytest.raises((EOFError, OSError)):
        restore(live, io.BytesIO(good[: len(good) // 2]))
    assert len(names(live)) == 4


def test_restore_can_replace_a_corrupt_live_database(tmp_path: Path) -> None:
    live = tmp_path / "app.db"
    live.write_bytes(b"corrupt" * 300)
    data = backup_bytes(make_db(tmp_path / "good.db", rows=2))
    safety = restore(live, io.BytesIO(data))
    assert names(live) == ["n0", "n1"]
    assert safety is not None
    assert safety.read_bytes().startswith(b"corrupt")


def test_check_integrity_flags_damage(tmp_path: Path) -> None:
    path = make_db(tmp_path / "app.db", rows=2000)
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    raw = bytearray(path.read_bytes())
    raw[4096 * 2 : 4096 * 2 + 200] = b"\xff" * 200
    path.write_bytes(bytes(raw))
    with pytest.raises(DataError):
        check_integrity(path)


def test_cli_backup_verify_restore_roundtrip(tmp_path: Path) -> None:
    source = make_db(tmp_path / "app.db", rows=4)
    archive = tmp_path / "b" / "app.db.gz"
    assert main(["backup", "--db", str(source), "--output", str(archive)]) == 0
    assert archive.is_file()
    assert not list((tmp_path / "b").glob("*.part"))
    assert main(["verify", "--input", str(archive)]) == 0
    target = tmp_path / "t" / "app.db"
    assert main(["restore", "--db", str(target), "--input", str(archive)]) == 0
    assert names(target) == ["n0", "n1", "n2", "n3"]


def test_cli_reports_failures_and_usage_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["backup", "--db", str(tmp_path / "x.db"), "--output", str(tmp_path / "o")]) == 1
    assert "error:" in capsys.readouterr().err
    assert main(["verify", "--input", str(tmp_path / "missing.gz")]) == 1
    assert main(["bogus"]) == 2
    assert main(["--help"]) == 0


def test_cli_stdout_and_stdin_streams(tmp_path: Path) -> None:
    """The ops scripts pipe through stdout/stdin; exercise the real module entry point."""
    source = make_db(tmp_path / "app.db", rows=3)
    dumped = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "app.ops.sqlite_tool", "backup", "--db", str(source)],
        capture_output=True,
        check=True,
    )
    assert gzip.decompress(dumped.stdout)[:15] == b"SQLite format 3"
    target = tmp_path / "r" / "app.db"
    restored = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "app.ops.sqlite_tool", "restore", "--db", str(target)],
        input=dumped.stdout,
        capture_output=True,
        check=True,
    )
    assert b"restored" in restored.stderr
    assert names(target) == ["n0", "n1", "n2"]


@pytest.fixture
def forbidden_system_tmp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if a database copy would be placed in the system temp directory (tmpfs)."""
    original = tempfile.mkdtemp

    def guarded(
        suffix: str | None = None,
        prefix: str | None = None,
        dir: str | None = None,  # noqa: A002 - mirrors tempfile.mkdtemp
    ) -> str:
        if dir is None:
            msg = "temporary directory requested without dir=: would land in /tmp"
            raise AssertionError(msg)
        return original(suffix, prefix, dir)

    monkeypatch.setattr(tempfile, "mkdtemp", guarded)


@pytest.mark.usefixtures("forbidden_system_tmp")
def test_backup_scratch_space_is_next_to_the_database(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    source = make_db(tmp_path / "data" / "app.db")
    archive = backup_bytes(source)
    assert archive
    assert not [p for p in source.parent.iterdir() if p.name.startswith(".backup-")]


@pytest.mark.usefixtures("forbidden_system_tmp")
def test_verify_scratch_space_is_the_given_work_dir(tmp_path: Path) -> None:
    archive = backup_bytes(make_db(tmp_path / "app.db"))
    work_dir = tmp_path / "volume" / "scratch"
    assert "abc123" in verify(io.BytesIO(archive), work_dir)
    assert not list(work_dir.iterdir())


def test_restore_keeps_only_the_newest_safety_copies(tmp_path: Path) -> None:
    live = make_db(tmp_path / "app.db", rows=1)
    safety_dir = tmp_path / "pre-restore"
    safety_dir.mkdir()
    for day in range(1, 9):
        (safety_dir / f"app-before-restore-2026010{day}T000000Z.db").write_bytes(b"old")
    other = safety_dir / "notes.txt"
    other.write_text("keep me")

    restore(live, io.BytesIO(backup_bytes(make_db(tmp_path / "b.db"))))

    kept = sorted(p.name for p in safety_dir.glob("*-before-restore-*.db"))
    assert len(kept) == 5
    assert "app-before-restore-20260101T000000Z.db" not in kept
    assert "app-before-restore-20260108T000000Z.db" in kept
    assert other.exists()
