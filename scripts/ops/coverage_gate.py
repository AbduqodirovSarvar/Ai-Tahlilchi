"""Per-package coverage gate (run after pytest has produced `.coverage`).

pytest-cov enforces only one global threshold (85%, see pyproject.toml). CLAUDE.md also
requires >= 90% for the core logic packages; this script enforces those. A package that
has no measured code yet passes, so the gate can be on from day one.

Usage: uv run python scripts/ops/coverage_gate.py
"""

import io
import sys
from typing import Final

from coverage import Coverage
from coverage.exceptions import NoDataError

PACKAGE_THRESHOLDS: Final[dict[str, float]] = {
    "*/app/models/*": 90.0,
    "*/app/pipeline/*": 90.0,
}


def package_coverage(cov: Coverage, pattern: str) -> float | None:
    """Return the coverage percentage of files matching ``pattern``, or None if no code."""
    try:
        return cov.report(include=[pattern], file=io.StringIO())
    except NoDataError:
        return None


def main() -> int:
    """Check every package threshold and return a process exit code."""
    cov = Coverage()
    cov.load()
    failed = False
    for pattern, threshold in PACKAGE_THRESHOLDS.items():
        percent = package_coverage(cov, pattern)
        if percent is None:
            print(f"SKIP {pattern}: no measured code yet")
            continue
        status = "OK  " if percent >= threshold else "FAIL"
        failed = failed or percent < threshold
        print(f"{status} {pattern}: {percent:.2f}% (required {threshold:.0f}%)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
