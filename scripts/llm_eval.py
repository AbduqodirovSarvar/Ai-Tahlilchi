"""F5.6: run the golden set against the LIVE Gemini API (manual, needs a key).

Usage (needs ``GEMINI_API_KEY`` in ``.env``; costs about one request per golden case):

    uv run python scripts/llm_eval.py
    uv run python scripts/llm_eval.py --delay 7      # stay under free-tier requests/minute

Run it after every prompt change and compare ``docs/llm_eval/<date>.md`` files. Cases marked
``recorded_only`` test code paths with scripted answers and are skipped here. CI never runs
this script; it exercises the same cases with the recorded answers instead.

Exit codes: 0 all cases passed, 1 at least one case failed or the API failed, 2 no key or
bad arguments.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from app.clients.gemini import create_gemini_client
from app.config import get_settings
from app.errors import ConfigurationError
from app.llm.evaluation import CaseResult, check_case, load_golden_cases, render_report
from app.llm.service import LlmService
from app.logging import configure_logging, get_logger

logger = get_logger(__name__)

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_CONFIGURATION_ERROR: Final = 2
REPO_ROOT: Final = Path(__file__).resolve().parents[1]
GOLDEN_DIR: Final = REPO_ROOT / "tests" / "llm" / "golden"
REPORT_DIR: Final = REPO_ROOT / "docs" / "llm_eval"
NO_KEY_MESSAGE: Final = (
    "GEMINI_API_KEY is not set: add it to .env (see .env.example). "
    "The live evaluation cannot run without it; CI uses the recorded answers instead."
)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Command line options."""
    parser = argparse.ArgumentParser(description="Run the golden LLM cases against live Gemini.")
    parser.add_argument("--golden", type=Path, default=GOLDEN_DIR, help="directory of case files")
    parser.add_argument("--report-dir", type=Path, default=REPORT_DIR)
    parser.add_argument("--delay", type=float, default=0.0, help="seconds between requests")
    return parser.parse_args(list(argv))


async def run(args: argparse.Namespace, service: LlmService, model: str) -> list[CaseResult]:
    """Evaluate every live-capable case sequentially (one request each)."""
    results: list[CaseResult] = []
    for case in load_golden_cases(args.golden):
        if case.recorded_only:
            continue
        reviews = await service.review_candidates(case.candidates, case.context)
        result = check_case(case, reviews)
        logger.info("llm_eval_case", case=case.id, passed=result.passed, model=model)
        results.append(result)
        await asyncio.sleep(args.delay)
    return results


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the process exit code."""
    configure_logging(level="INFO", app_env="dev")
    try:
        settings = get_settings()
        configure_logging(level="INFO", app_env="dev", secret_values=settings.secret_values())
        args = parse_args(sys.argv[1:] if argv is None else argv)
        client = create_gemini_client(settings)
    except ConfigurationError as exc:
        reason = NO_KEY_MESSAGE if "GEMINI_API_KEY" in str(exc) else str(exc)
        logger.error("llm_eval_not_run", reason=reason)
        return EXIT_CONFIGURATION_ERROR
    service = LlmService(client)
    results = asyncio.run(run(args, service, client.model))
    today = datetime.now(UTC).date()
    prompt_version = results[0].reviews[0].prompt_version if results else "-"
    report = render_report(results, model=client.model, prompt_version=prompt_version, day=today)
    args.report_dir.mkdir(parents=True, exist_ok=True)
    path = args.report_dir / f"{today.isoformat()}.md"
    path.write_text(report, encoding="utf-8")
    failed = [result.case_id for result in results if not result.passed]
    logger.info("llm_eval_finished", report=str(path), cases=len(results), failed=failed)
    return EXIT_FAILED if failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
