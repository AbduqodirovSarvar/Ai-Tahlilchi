"""Print matches joined with their football-data.co.uk Bet365 odds as JSON lines.

Usage (``FOOTBALL_DATA_ORG_KEY`` in ``.env``):

    uv run python scripts/aggregate_matches.py                       # today .. +2 days
    uv run python scripts/aggregate_matches.py --from 2026-10-31 --to 2026-11-02

Exit codes: 0 done, 1 Football-Data.org failed, 2 bad arguments or configuration.
"""

import argparse
import asyncio
import io
import json
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Final

from app.clients.football_data_org_models import TeamLineup
from app.config import get_settings
from app.errors import ConfigurationError, ExternalServiceError
from app.logging import configure_logging, get_logger
from app.pipeline.aggregator import open_data_aggregator
from app.sports.aggregator import CombinedMatchDto

logger = get_logger(__name__)

EXIT_OK: Final = 0
EXIT_PROVIDER_ERROR: Final = 1
EXIT_CONFIGURATION_ERROR: Final = 2
DEFAULT_DAYS: Final = 3


def to_json(match: CombinedMatchDto) -> dict[str, object]:
    """A JSON-ready view of one combined match."""
    odds = match.odds
    return {
        "match_id": match.match_id,
        "match_date": match.match_date.isoformat(),
        "league": match.league,
        "home_team": match.home_team,
        "away_team": match.away_team,
        "home_lineup": _lineup(match.home_lineup),
        "away_lineup": _lineup(match.away_lineup),
        "odds": None
        if odds is None
        else {"home_win": odds.home_win, "draw": odds.draw, "away_win": odds.away_win},
        "link": match.link.value,
    }


def _lineup(lineup: TeamLineup | None) -> list[str] | None:
    return None if lineup is None else [player.name or "?" for player in lineup.starters]


async def run(day_from: date, day_to: date) -> None:
    """Aggregate the range and write one JSON object per line."""
    async with open_data_aggregator(get_settings()) as aggregator:
        matches = await aggregator.combined_matches(day_from, day_to)
    for match in matches:
        sys.stdout.write(json.dumps(to_json(match), ensure_ascii=False) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the process exit code."""
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="backslashreplace")
    configure_logging(level="INFO", app_env="dev")
    today = datetime.now(UTC).date()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="day_from", type=date.fromisoformat, default=today)
    parser.add_argument(
        "--to",
        dest="day_to",
        type=date.fromisoformat,
        default=today + timedelta(days=DEFAULT_DAYS - 1),
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        asyncio.run(run(args.day_from, args.day_to))
    except (ConfigurationError, ValueError) as exc:
        logger.critical("aggregate_aborted", reason=str(exc))
        return EXIT_CONFIGURATION_ERROR
    except ExternalServiceError as exc:
        logger.error("aggregate_failed", reason=str(exc))
        return EXIT_PROVIDER_ERROR
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
