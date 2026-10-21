"""Per-package coverage gate (run after pytest has produced `.coverage`).

pytest-cov enforces only one global threshold (85%, see pyproject.toml). CLAUDE.md also
requires >= 90% for the core logic packages; this script enforces those.

- Missing coverage data, or data without any measured file, is an error (exit 1): the gate
  must never pass because pytest did not run.
- A package that has no statements yet is reported as SKIP, so the gate can be on from day
  one without pretending an empty package is "100% covered".

Usage: uv run python scripts/ops/coverage_gate.py
"""

import json
import sys
import tempfile
from pathlib import Path
from typing import Final

from coverage import Coverage
from coverage.exceptions import NoDataError

PACKAGE_THRESHOLDS: Final[dict[str, float]] = {
    "*/app/models/*": 90.0,
    "*/app/pipeline/*": 90.0,
}


def package_totals(cov: Coverage, pattern: str) -> tuple[int, float] | None:
    """Return (statement count, percent covered) for files matching ``pattern``.

    Unimported source files count too (``[tool.coverage.run] source``). None if no file matches.
    """
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "coverage.json"
        try:
            cov.json_report(include=[pattern], outfile=str(report))
        except NoDataError:
            return None
        totals = json.loads(report.read_text(encoding="utf-8"))["totals"]
    return int(totals["num_statements"]), float(totals["percent_covered"])


def load_coverage() -> Coverage | None:
    """Load the data pytest wrote, or explain why there is none."""
    cov = Coverage()
    data_file = Path(cov.config.data_file)
    if not data_file.is_file():
        print(f"ERROR coverage data {data_file} not found; run `uv run pytest` first")
        return None
    cov.load()
    if not cov.get_data().measured_files():
        print(f"ERROR coverage data {data_file} contains no measured files")
        return None
    return cov


def main() -> int:
    """Check every package threshold and return a process exit code."""
    cov = load_coverage()
    if cov is None:
        return 1
    failed = False
    for pattern, threshold in PACKAGE_THRESHOLDS.items():
        totals = package_totals(cov, pattern)
        if totals is None or totals[0] == 0:
            print(f"SKIP {pattern}: no statements yet")
            continue
        statements, percent = totals
        passed = percent >= threshold
        failed = failed or not passed
        status = "OK  " if passed else "FAIL"
        print(
            f"{status} {pattern}: {percent:.2f}% of {statements} statements (min {threshold:.0f}%)"
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
