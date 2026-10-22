"""Health probe: database, scheduler liveness (real APScheduler job store) and disk."""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.ops import healthcheck
from app.ops.healthcheck import (
    TICK_JOB_ID,
    check_database,
    check_disk,
    check_scheduler,
    last_runs,
    run_checks,
)
from app.scheduler import JOB_DAILY_TICK, execute_job

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


def make_db(path: Path) -> Path:
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE t (x)")
        db.commit()
    return path


def make_jobs_table(path: Path, next_run: datetime | None, job_id: str = TICK_JOB_ID) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE IF NOT EXISTS apscheduler_jobs (id TEXT, next_run_time REAL)")
        db.execute(
            "INSERT INTO apscheduler_jobs VALUES (?, ?)",
            (job_id, None if next_run is None else next_run.timestamp()),
        )
        db.commit()


def test_job_id_matches_the_scheduler() -> None:
    assert healthcheck.TICK_JOB_ID == JOB_DAILY_TICK


def test_missing_database_is_unhealthy(tmp_path: Path) -> None:
    result = check_database(tmp_path / "none.db")
    assert not result.ok
    assert "does not exist" in result.detail


def test_garbage_file_is_unhealthy(tmp_path: Path) -> None:
    path = tmp_path / "bad.db"
    path.write_bytes(b"this is not sqlite" * 100)
    assert not check_database(path).ok


def test_healthy_database(tmp_path: Path) -> None:
    assert check_database(make_db(tmp_path / "a.db")).ok


@pytest.mark.parametrize(
    ("lag", "ok"),
    [
        (timedelta(minutes=-1), True),
        (timedelta(minutes=4), True),
        (timedelta(minutes=6), False),
        (timedelta(hours=3), False),
    ],
)
def test_scheduler_lag(tmp_path: Path, lag: timedelta, ok: bool) -> None:
    path = make_db(tmp_path / "a.db")
    make_jobs_table(path, NOW - lag)
    assert check_scheduler(path, now=NOW, max_lag_minutes=5).ok is ok


def test_scheduler_without_job_table_or_job_or_paused(tmp_path: Path) -> None:
    path = make_db(tmp_path / "a.db")
    assert "unreadable" in check_scheduler(path, now=NOW, max_lag_minutes=5).detail
    make_jobs_table(path, NOW, job_id="other")
    assert "not registered" in check_scheduler(path, now=NOW, max_lag_minutes=5).detail
    other = make_db(tmp_path / "b.db")
    make_jobs_table(other, None)
    assert "paused" in check_scheduler(other, now=NOW, max_lag_minutes=5).detail


async def test_a_live_apscheduler_store_is_healthy_and_a_stale_one_is_not(tmp_path: Path) -> None:
    """The probe relies on APScheduler's real table layout, so test against the real thing."""
    path = tmp_path / "jobs.db"
    scheduler = AsyncIOScheduler(jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{path}")})
    scheduler.start(paused=True)
    scheduler.add_job(execute_job, "interval", seconds=60, id=TICK_JOB_ID, args=["x"])
    scheduler.shutdown(wait=False)

    now = datetime.now(UTC)
    assert check_scheduler(path, now=now, max_lag_minutes=5).ok
    assert not check_scheduler(path, now=now + timedelta(hours=1), max_lag_minutes=5).ok


def test_disk_threshold(tmp_path: Path) -> None:
    path = make_db(tmp_path / "a.db")
    assert check_disk(path, min_free_mb=1).ok
    low = check_disk(path, min_free_mb=10**9)
    assert not low.ok
    assert "free" in low.detail


def test_disk_check_survives_a_missing_directory(tmp_path: Path) -> None:
    assert check_disk(tmp_path / "gone" / "a.db", min_free_mb=1).ok


def test_scheduler_is_checked_only_for_real_services(tmp_path: Path) -> None:
    path = make_db(tmp_path / "a.db")
    fake = run_checks(path, expect_scheduler=False, now=NOW)
    real = run_checks(path, expect_scheduler=True, now=NOW)
    assert [c.name for c in fake] == ["database", "disk"]
    assert [c.name for c in real] == ["database", "scheduler", "disk"]
    assert not all(c.ok for c in real)


def test_missing_database_skips_the_scheduler_check(tmp_path: Path) -> None:
    results = run_checks(tmp_path / "none.db", expect_scheduler=True, now=NOW)
    assert [c.name for c in results] == ["database", "disk"]


def test_last_runs_reads_the_ledger_or_nothing(tmp_path: Path) -> None:
    path = make_db(tmp_path / "a.db")
    assert last_runs(path) == {}
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE pipeline_ledger (job, subject, status, done_at)")
        db.execute("INSERT INTO pipeline_ledger VALUES ('run.refresh','refresh','done','t1')")
        db.execute("INSERT INTO pipeline_ledger VALUES ('daily','x','done','t2')")
        db.commit()
    assert last_runs(path) == {"refresh": "done t1"}


def test_main_reports_json_and_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = make_db(tmp_path / "a.db")
    monkeypatch.setenv("DB_PATH", str(path))
    healthcheck.get_settings.cache_clear()

    assert healthcheck.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["healthy"] is True

    monkeypatch.setenv("APP_SERVICES", "real")
    healthcheck.get_settings.cache_clear()
    assert healthcheck.main([]) == 1
    assert json.loads(capsys.readouterr().out)["healthy"] is False
    healthcheck.get_settings.cache_clear()


def test_main_with_invalid_settings_is_unhealthy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "LOUD")
    healthcheck.get_settings.cache_clear()
    assert healthcheck.main([]) == 1
    assert json.loads(capsys.readouterr().out)["healthy"] is False
    healthcheck.get_settings.cache_clear()
