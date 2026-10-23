"""Container health probe (``python -m app.ops.healthcheck``), used by Docker's HEALTHCHECK.

Healthy means: no configuration error is pending, the database file opens and answers, the
scheduler is alive (with the real services) and the disk has room. Exit code 0 healthy,
1 unhealthy; one JSON line on stdout (also shown by ``docker inspect``) explains every check.

Scheduler liveness without touching application code: APScheduler stores each job's
``next_run_time`` in the job store after every run, and the ``daily-tick`` job fires every
minute (in the scheduler's own ``jobs.db``). A live scheduler therefore always has that time
in the future or just behind; a stuck event loop or a dead scheduler leaves it further and
further in the past. Bot polling runs in the same process and event loop, so a hung loop
fails this check too, and a crashed polling task ends the process, which Docker restarts. The
in-app watchdog (:mod:`app.ops.watchdog`) additionally restarts a process whose scheduler
stopped ticking.

The probe only reads (``mode=ro``) and imports nothing heavy, so it is cheap to run every
minute and cannot compete with the application for the write lock.
"""

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from app.config import JOBS_DB_NAME, get_settings
from app.errors import ConfigurationError

EXIT_HEALTHY: Final = 0
EXIT_UNHEALTHY: Final = 1
TICK_JOB_ID: Final = "daily-tick"
"""Must equal ``app.scheduler.JOB_DAILY_TICK`` (a test pins it)."""
JOBS_TABLE: Final = "apscheduler_jobs"
DEFAULT_MAX_TICK_LAG_MINUTES: Final = 5.0
DEFAULT_MIN_FREE_MB: Final = 200
BYTES_PER_MB: Final = 1024 * 1024
DB_TIMEOUT_SECONDS: Final = 3.0
CONFIG_ERROR_MARKER_NAME: Final = "ai-tahlilchi-config-error"
"""Created by ``deploy/entrypoint.sh`` while it waits after exit code 2 (configuration error)."""
RUN_PREFIX: Final = "run."
"""Ledger prefix of per-job last-run records (``app.pipeline.ledger.RUN_PREFIX``)."""


@dataclass(frozen=True, slots=True)
class CheckResult:
    """The outcome of one check."""

    name: str
    ok: bool
    detail: str


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=DB_TIMEOUT_SECONDS)


def check_config_marker(directory: Path | None = None) -> CheckResult:
    """No configuration error is being waited out (the entrypoint leaves a marker file)."""
    marker = (directory or Path(tempfile.gettempdir())) / CONFIG_ERROR_MARKER_NAME
    if marker.exists():
        return CheckResult("config", ok=False, detail="startup failed with a configuration error")
    return CheckResult("config", ok=True, detail="no configuration error")


def check_database(path: Path) -> CheckResult:
    """The database file opens read-only and answers a query."""
    if not path.is_file():
        return CheckResult("database", ok=False, detail=f"{path} does not exist")
    try:
        with closing(_connect(path)) as db:
            tables = db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
    except sqlite3.Error as error:
        return CheckResult("database", ok=False, detail=f"{type(error).__name__}: {error}")
    return CheckResult("database", ok=True, detail=f"{tables} schema objects")


def check_scheduler(path: Path, *, now: datetime, max_lag_minutes: float) -> CheckResult:
    """The tick job's next run is not overdue by more than ``max_lag_minutes``."""
    try:
        with closing(_connect(path)) as db:
            row = db.execute(
                f"SELECT next_run_time FROM {JOBS_TABLE} WHERE id = ?",  # noqa: S608 - constant name
                (TICK_JOB_ID,),
            ).fetchone()
    except sqlite3.Error as error:
        return CheckResult("scheduler", ok=False, detail=f"job store unreadable: {error}")
    if row is None:
        return CheckResult("scheduler", ok=False, detail=f"job {TICK_JOB_ID} is not registered")
    if row[0] is None:
        return CheckResult("scheduler", ok=False, detail=f"job {TICK_JOB_ID} is paused")
    lag_minutes = (now.timestamp() - float(row[0])) / 60
    detail = f"{TICK_JOB_ID} lag {max(lag_minutes, 0.0):.1f} min (limit {max_lag_minutes:g})"
    return CheckResult("scheduler", ok=lag_minutes <= max_lag_minutes, detail=detail)


def check_disk(path: Path, *, min_free_mb: int) -> CheckResult:
    """The volume holding the database has at least ``min_free_mb`` free; sizes are reported."""
    usage = shutil.disk_usage(path.parent if path.parent.exists() else Path.cwd())
    free_mb = usage.free // BYTES_PER_MB
    sizes = {
        suffix or "db": path.with_name(path.name + suffix).stat().st_size // BYTES_PER_MB
        for suffix in ("", "-wal")
        if path.with_name(path.name + suffix).exists()
    }
    detail = f"free {free_mb} MB (min {min_free_mb}); file sizes MB {sizes}"
    return CheckResult("disk", ok=free_mb >= min_free_mb, detail=detail)


def last_runs(path: Path) -> dict[str, str]:
    """When each job last ended and how (information only; empty when unreadable)."""
    try:
        with closing(_connect(path)) as db:
            rows = db.execute(
                "SELECT subject, status, done_at FROM pipeline_ledger WHERE job LIKE ?",
                (f"{RUN_PREFIX}%",),
            ).fetchall()
    except sqlite3.Error:
        return {}
    return {str(subject): f"{status} {done_at}" for subject, status, done_at in rows}


@dataclass(frozen=True, slots=True)
class Limits:
    """Thresholds of the probe."""

    max_tick_lag_minutes: float = DEFAULT_MAX_TICK_LAG_MINUTES
    min_free_mb: int = DEFAULT_MIN_FREE_MB


def run_checks(
    path: Path,
    *,
    expect_scheduler: bool,
    now: datetime,
    jobs_path: Path | None = None,
    limits: Limits = Limits(),  # noqa: B008 - frozen, shared on purpose
) -> list[CheckResult]:
    """All checks in order; the scheduler is checked only when the real services run."""
    results = [check_config_marker(), check_database(path)]
    if results[1].ok and expect_scheduler:
        results.append(
            check_scheduler(
                jobs_path or path.with_name(JOBS_DB_NAME),
                now=now,
                max_lag_minutes=limits.max_tick_lag_minutes,
            )
        )
    results.append(check_disk(path, min_free_mb=limits.min_free_mb))
    return results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.ops.healthcheck", description=__doc__)
    parser.add_argument("--max-tick-lag-minutes", type=float, default=DEFAULT_MAX_TICK_LAG_MINUTES)
    parser.add_argument("--min-free-mb", type=int, default=DEFAULT_MIN_FREE_MB)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Probe and print a JSON report; the exit code is the verdict."""
    arguments = _parser().parse_args(argv)
    try:
        settings = get_settings()
    except ConfigurationError as error:
        sys.stdout.write(json.dumps({"healthy": False, "error": str(error)}) + "\n")
        return EXIT_UNHEALTHY
    results = run_checks(
        settings.db_path,
        expect_scheduler=settings.app_services == "real",
        now=datetime.now(UTC),
        jobs_path=settings.jobs_db_path,
        limits=Limits(arguments.max_tick_lag_minutes, arguments.min_free_mb),
    )
    healthy = all(result.ok for result in results)
    report = {
        "healthy": healthy,
        "checks": [asdict(result) for result in results],
        "last_runs": last_runs(settings.db_path),
    }
    sys.stdout.write(json.dumps(report, ensure_ascii=False) + "\n")
    return EXIT_HEALTHY if healthy else EXIT_UNHEALTHY


if __name__ == "__main__":
    sys.exit(main())
