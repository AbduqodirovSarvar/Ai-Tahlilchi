"""Backup, verify and restore of the SQLite database (``python -m app.ops.sqlite_tool``).

The ops scripts (``scripts/ops/backup.sh``, ``restore.sh``) call this module inside the
application container, so no ``sqlite3`` command-line tool is needed on the host and the
logic is covered by tests instead of living in shell.

Safety properties:

- **Online backup:** ``sqlite3.Connection.backup`` copies a consistent snapshot while the bot
  keeps writing (the same mechanism as the CLI ``.backup``); a plain file copy of a WAL
  database could be torn.
- **Verified before it counts:** the copy is checked with ``PRAGMA integrity_check`` before
  it is compressed; a corrupt snapshot fails the command and leaves no file behind.
- **Restore is reversible:** the current database is first copied to a safety file, the
  restored file is checked before it replaces anything, and the replacement is one atomic
  rename; stale ``-wal``/``-shm`` files are removed so they cannot be replayed onto it.

``query`` answers "what is in this table?" without a ``sqlite3`` binary on the host.

Exit codes: 0 success, 1 operation failed, 2 bad usage.
"""

import argparse
import gzip
import shutil
import sqlite3
import sys
import tempfile
import zlib
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Final, TextIO

from app.errors import DataError

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_USAGE: Final = 2
STDIO: Final = "-"
GZIP_MAGIC: Final = b"\x1f\x8b"
SAFETY_DIR_NAME: Final = "pre-restore"
SAFETY_COPIES_KEPT: Final = 5
"""Before-restore copies kept; older ones are deleted so repeated restores cannot fill the disk."""
CHUNK_BYTES: Final = 1024 * 1024
LOCK_WAIT_SECONDS: Final = 30.0
QUERY_MAX_ROWS: Final = 1000


def check_integrity(path: Path) -> None:
    """Raise :class:`DataError` unless SQLite reports the file at ``path`` as intact."""
    try:
        with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as db:
            rows = db.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error as error:
        msg = f"{path.name} is not a readable SQLite database: {error}"
        raise DataError(msg) from error
    if rows != [("ok",)]:
        problems = "; ".join(str(row[0]) for row in rows[:5])
        msg = f"integrity_check failed for {path.name}: {problems}"
        raise DataError(msg)


def snapshot(source: Path, destination: Path) -> None:
    """Write a consistent copy of the live database ``source`` to ``destination``."""
    if not source.is_file():
        msg = f"database {source} does not exist"
        raise DataError(msg)
    try:
        with (
            closing(
                sqlite3.connect(
                    f"file:{source.as_posix()}?mode=ro", uri=True, timeout=LOCK_WAIT_SECONDS
                )
            ) as live,
            closing(sqlite3.connect(destination)) as copy,
        ):
            live.backup(copy)
    except sqlite3.Error as error:
        msg = f"online backup of {source.name} failed: {error}"
        raise DataError(msg) from error


def backup(database: Path, output: BinaryIO) -> None:
    """Stream a verified, gzip-compressed snapshot of ``database`` into ``output``."""
    # Scratch space next to the database (the data volume), never /tmp: in the container /tmp is
    # a tmpfs that counts against the memory limit, and a database copy there would sit in RAM.
    with tempfile.TemporaryDirectory(prefix=".backup-", dir=database.parent) as scratch:
        copy = Path(scratch) / "snapshot.db"
        snapshot(database, copy)
        check_integrity(copy)
        with copy.open("rb") as raw, gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as packed:
            shutil.copyfileobj(raw, packed, CHUNK_BYTES)


def backup_to_file(database: Path, destination: Path) -> None:
    """:func:`backup` into ``destination`` via a temp file: no partial backup is ever visible."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    try:
        with partial.open("wb") as handle:
            backup(database, handle)
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)


def unpack(source: BinaryIO, destination: Path) -> None:
    """Copy ``source`` (gzip or a plain database) to ``destination``."""
    head = source.read(len(GZIP_MAGIC))
    with destination.open("wb") as target:
        if head == GZIP_MAGIC:
            with gzip.GzipFile(fileobj=_Prefixed(head, source), mode="rb") as packed:  # type: ignore[call-overload]  # duck-types the read-only file object
                shutil.copyfileobj(packed, target, CHUNK_BYTES)
        else:
            target.write(head)
            shutil.copyfileobj(source, target, CHUNK_BYTES)


class _Prefixed:
    """A read-only file object that replays ``head`` before the rest of ``source``."""

    def __init__(self, head: bytes, source: BinaryIO) -> None:
        self._head = head
        self._source = source

    def read(self, size: int = -1) -> bytes:
        """Return up to ``size`` bytes, the replayed prefix first."""
        if self._head:
            chunk, self._head = self._head, b""
            rest = self._source.read(size - len(chunk) if size >= 0 else -1)
            return chunk + rest
        return self._source.read(size)


def verify(source: BinaryIO, work_dir: Path | None = None) -> str:
    """Check a backup (gzip or plain) and describe it: ``ok`` plus the Alembic revision.

    ``work_dir`` should be on the data volume (see :func:`backup`); the default is the system
    temp directory, which suits tests and hosts with a disk-backed ``/tmp``.
    """
    if work_dir is not None:
        work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".verify-", dir=work_dir) as scratch:
        copy = Path(scratch) / "backup.db"
        unpack(source, copy)
        check_integrity(copy)
        return f"integrity_check=ok alembic_revision={alembic_revision(copy)}"


def alembic_revision(path: Path) -> str:
    """The migration revision stored in ``path``, or ``none`` for an unmigrated database."""
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as db:
        try:
            row = db.execute("SELECT version_num FROM alembic_version").fetchone()
        except sqlite3.OperationalError:
            return "none"
    return "none" if row is None else str(row[0])


def restore(database: Path, source: BinaryIO, safety_dir: Path | None = None) -> Path | None:
    """Replace ``database`` with the backup read from ``source``; return the safety copy.

    The caller must have stopped the application: a running process would keep writing to
    the old file through its open handles.
    """
    safety_dir = safety_dir or database.parent / SAFETY_DIR_NAME
    database.parent.mkdir(parents=True, exist_ok=True)
    staged = database.with_name(database.name + ".restoring")
    try:
        unpack(source, staged)
        check_integrity(staged)
        safety = _safety_copy(database, safety_dir)
        _prune_safety_copies(safety_dir, keep=SAFETY_COPIES_KEPT)
        for suffix in ("-wal", "-shm"):
            database.with_name(database.name + suffix).unlink(missing_ok=True)
        staged.replace(database)
    finally:
        staged.unlink(missing_ok=True)
    return safety


def _safety_copy(database: Path, safety_dir: Path) -> Path | None:
    """Keep the database that is about to be replaced; None when there is none."""
    if not database.is_file():
        return None
    safety_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = safety_dir / f"{database.stem}-before-restore-{stamp}.db"
    try:
        snapshot(database, target)
    except DataError:
        # A corrupt database cannot be snapshotted; copy the bytes, that is what we have.
        shutil.copy2(database, target)
    return target


def _prune_safety_copies(safety_dir: Path, *, keep: int) -> None:
    """Delete all but the ``keep`` newest before-restore copies (names sort by timestamp)."""
    copies = sorted(safety_dir.glob("*-before-restore-*.db"), reverse=True)
    for stale in copies[keep:]:
        stale.unlink(missing_ok=True)


def query(database: Path, sql: str, output: TextIO) -> int:
    """Print the rows of one read-only statement as TSV (header first); returns the row count.

    The ops answer to "what is in the table?" on a server that has no ``sqlite3`` binary. The
    connection is read-only and ``query_only``, so a typo cannot change data; output is capped
    at :data:`QUERY_MAX_ROWS` rows so a forgotten ``LIMIT`` cannot flood a terminal.
    """
    if not database.is_file():
        msg = f"database {database} does not exist"
        raise DataError(msg)
    try:
        with closing(
            sqlite3.connect(
                f"file:{database.as_posix()}?mode=ro", uri=True, timeout=LOCK_WAIT_SECONDS
            )
        ) as db:
            db.execute("PRAGMA query_only = ON")
            cursor = db.execute(sql)
            columns = [column[0] for column in cursor.description or ()]
            rows = cursor.fetchmany(QUERY_MAX_ROWS)
    except sqlite3.Error as error:
        msg = f"query failed: {error}"
        raise DataError(msg) from error
    output.write("\t".join(columns) + "\n")
    for row in rows:
        output.write("\t".join("" if value is None else str(value) for value in row) + "\n")
    return len(rows)


def _open_input(argument: str) -> BinaryIO:
    return sys.stdin.buffer if argument == STDIO else Path(argument).open("rb")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.ops.sqlite_tool", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup_cmd = commands.add_parser("backup", help="verified gzip snapshot of a live database")
    backup_cmd.add_argument("--db", type=Path, required=True)
    backup_cmd.add_argument("--output", default=STDIO, help="file path, or - for stdout")
    query_cmd = commands.add_parser("query", help="run one read-only SELECT, print TSV")
    query_cmd.add_argument("--db", type=Path, required=True)
    query_cmd.add_argument("sql", help="a single SELECT (or WITH ... SELECT) statement")
    verify_cmd = commands.add_parser("verify", help="check a backup file (gzip or plain)")
    verify_cmd.add_argument("--input", default=STDIO, help="file path, or - for stdin")
    verify_cmd.add_argument(
        "--work-dir", type=Path, default=None, help="scratch directory (default: system temp)"
    )
    restore_cmd = commands.add_parser("restore", help="replace the database with a backup")
    restore_cmd.add_argument("--db", type=Path, required=True)
    restore_cmd.add_argument("--input", default=STDIO, help="file path, or - for stdin")
    restore_cmd.add_argument("--safety-dir", type=Path, default=None)
    return parser


def _run(arguments: argparse.Namespace) -> None:
    """Execute the parsed command; results go to stderr/stdout as documented per command."""
    if arguments.command == "backup":
        if arguments.output == STDIO:
            backup(arguments.db, sys.stdout.buffer)
        else:
            backup_to_file(arguments.db, Path(arguments.output))
    elif arguments.command == "query":
        query(arguments.db, arguments.sql, sys.stdout)
    elif arguments.command == "verify":
        with _open_input(arguments.input) as source:
            sys.stderr.write(verify(source, arguments.work_dir) + "\n")
    else:
        with _open_input(arguments.input) as source:
            safety = restore(arguments.db, source, arguments.safety_dir)
        sys.stderr.write(f"restored; previous database kept at {safety}\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command; every failure is a one-line message on stderr, never a traceback."""
    try:
        arguments = _parser().parse_args(argv)
    except SystemExit as exit_request:
        return EXIT_OK if exit_request.code in (0, None) else EXIT_USAGE
    try:
        _run(arguments)
    except (DataError, OSError, EOFError, zlib.error) as error:
        sys.stderr.write(f"error: {error}\n")
        return EXIT_FAILED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
