"""deploy/entrypoint.sh: migrate first, supervise the app, throttle configuration errors."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.ops.shell import BASH, ROOT, posix, requires_bash, run_script

ENTRYPOINT = ROOT / "deploy" / "entrypoint.sh"

pytestmark = requires_bash


def make_executable(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\n" + body, newline="\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """A PATH directory with a recording fake ``alembic``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    make_executable(
        bin_dir / "alembic",
        'echo "alembic $*" >> "$LOG"\n[ -z "${ALEMBIC_FAIL:-}" ] || exit 3\n',
    )
    return tmp_path


def env_for(sandbox: Path, **extra: str) -> dict[str, str]:
    path = f"{posix(sandbox / 'bin')}:{os.environ.get('PATH', '')}"
    return {
        "PATH": path,
        "LOG": posix(sandbox / "log.txt"),
        "TMPDIR": posix(sandbox),
        "CONFIG_ERROR_BACKOFF_SECONDS": "1",
        **extra,
    }


def app(sandbox: Path, exit_code: int) -> str:
    script = make_executable(
        sandbox / "app.sh", f'echo "app started" >> "$LOG"\nexit {exit_code}\n'
    )
    return posix(script)


def log(sandbox: Path) -> list[str]:
    path = sandbox / "log.txt"
    return path.read_text().splitlines() if path.exists() else []


def test_migrates_before_the_application_starts(sandbox: Path) -> None:
    result = run_script(ENTRYPOINT, app(sandbox, 0), env=env_for(sandbox))

    assert result.returncode == 0, result.stdout
    assert log(sandbox) == ["alembic -c /app/alembic.ini upgrade head", "app started"]


def test_failed_migration_prevents_the_start(sandbox: Path) -> None:
    result = run_script(ENTRYPOINT, app(sandbox, 0), env=env_for(sandbox, ALEMBIC_FAIL="1"))

    assert result.returncode == 1
    assert "migrations_failed" in result.stdout
    assert "app started" not in log(sandbox)


def test_unexpected_error_exit_code_is_passed_on_without_delay(sandbox: Path) -> None:
    started = time.monotonic()
    result = run_script(
        ENTRYPOINT, app(sandbox, 1), env=env_for(sandbox, CONFIG_ERROR_BACKOFF_SECONDS="30")
    )

    assert result.returncode == 1
    assert time.monotonic() - started < 20
    assert "config_error_backoff" not in result.stdout


def test_configuration_error_waits_then_exits_2_and_cleans_its_marker(sandbox: Path) -> None:
    started = time.monotonic()
    result = run_script(ENTRYPOINT, app(sandbox, 2), env=env_for(sandbox))

    assert result.returncode == 2
    assert "config_error_backoff" in result.stdout
    assert time.monotonic() - started >= 1
    assert not (sandbox / "ai-tahlilchi-config-error").exists()


def test_skip_migrations_switch(sandbox: Path) -> None:
    result = run_script(ENTRYPOINT, app(sandbox, 0), env=env_for(sandbox, SKIP_MIGRATIONS="1"))

    assert result.returncode == 0
    assert log(sandbox) == ["app started"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_sigterm_reaches_the_application_and_its_exit_code_wins(sandbox: Path) -> None:
    assert BASH is not None
    slow = make_executable(
        sandbox / "slow.sh",
        'trap \'echo "app got TERM" >> "$LOG"; exit 0\' TERM\n'
        'echo "app started" >> "$LOG"\nwhile true; do sleep 0.1; done\n',
    )
    process = subprocess.Popen(  # noqa: S603
        [BASH, posix(ENTRYPOINT), posix(slow)], env={**os.environ, **env_for(sandbox)}
    )
    for _ in range(100):
        if "app started" in log(sandbox):
            break
        time.sleep(0.1)
    process.send_signal(signal.SIGTERM)

    assert process.wait(timeout=20) == 0
    assert "app got TERM" in log(sandbox)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_sigterm_during_the_backoff_exits_promptly(sandbox: Path) -> None:
    assert BASH is not None
    process = subprocess.Popen(  # noqa: S603
        [BASH, posix(ENTRYPOINT), app(sandbox, 2)],
        env={**os.environ, **env_for(sandbox, CONFIG_ERROR_BACKOFF_SECONDS="60")},
        stdout=subprocess.DEVNULL,
    )
    marker = sandbox / "ai-tahlilchi-config-error"
    for _ in range(100):
        if marker.exists():
            break
        time.sleep(0.1)
    started = time.monotonic()
    process.send_signal(signal.SIGTERM)

    assert process.wait(timeout=20) == 2
    assert time.monotonic() - started < 10
