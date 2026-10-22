"""Helpers to run the repository's bash scripts from tests (skipped when bash is missing)."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def find_bash() -> str | None:
    """A POSIX bash; the WSL launcher in System32 is not usable with Windows paths."""
    found = shutil.which("bash")
    if found is None or "system32" in found.lower():
        return None
    return found


BASH = find_bash()
requires_bash = pytest.mark.skipif(BASH is None, reason="bash is not available")


def posix(path: Path) -> str:
    """Forward-slash form, accepted by Git Bash and Linux alike."""
    return path.as_posix()


def run_script(
    script: Path,
    *args: str,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``script`` with bash; the environment is minimal plus ``env``."""
    assert BASH is not None
    base = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", str(ROOT))}
    for name in ("SYSTEMROOT", "TEMP", "TMP", "TMPDIR"):
        if name in os.environ:
            base[name] = os.environ[name]
    base.update(env or {})
    return subprocess.run(  # noqa: S603
        [BASH, posix(script), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=base,
        check=False,
        timeout=120,
    )


def tool_command() -> str:
    """The sqlite tool as a shell command line (replaces the Docker invocation in tests)."""
    return f"{posix(Path(sys.executable))} -m app.ops.sqlite_tool"
