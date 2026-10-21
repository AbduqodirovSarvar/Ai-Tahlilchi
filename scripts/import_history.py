"""Import football-data.co.uk history (results + odds) into the application database.

Usage (the schema must exist: ``uv run alembic upgrade head``):

    uv run python scripts/import_history.py                     # default leagues/seasons
    uv run python scripts/import_history.py --leagues E0,SP1 --seasons 2023-2025
    uv run python scripts/import_history.py --offline           # only files in data/history

Each league season is downloaded once into ``data/history/<season>/<league>.csv`` (the
current season is refreshed when older than 12 h) and imported in its own transaction, so
a broken file never undoes the others. Re-running is safe: every write is an upsert.

Exit codes: 0 all files imported, 1 some file failed (the rest were imported), 2 bad
arguments or configuration (e.g. the schema is missing).
"""

import argparse
import asyncio
import io
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import httpx
from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db.engine import create_engine, create_session_factory
from app.db.history import season_coverage, upsert_batch
from app.errors import AppError, ConfigurationError, ExternalServiceError
from app.logging import configure_logging, get_logger
from app.sports.history.download import (
    cache_path,
    create_client,
    current_season,
    download_season,
    needs_download,
)
from app.sports.history.football_data import LEAGUES, SOURCE, league_for, parse_season_csv

logger = get_logger("scripts.import_history")

# Orchestrator decision (docs/decisions_log.md): top-10 leagues, 5 complete seasons + current.
DEFAULT_LEAGUES: Final = ("E0", "E1", "SP1", "D1", "I1", "F1", "N1", "P1", "B1", "T1")
DEFAULT_FIRST_SEASON: Final = 2021
DEFAULT_CACHE_DIR: Final = Path("data/history")
MAX_LOGGED_SKIPPED_ROWS: Final = 5
EXIT_OK: Final = 0
EXIT_PARTIAL_FAILURE: Final = 1
EXIT_CONFIGURATION_ERROR: Final = 2


@dataclass(frozen=True, slots=True)
class ImportOptions:
    """What to import and from where."""

    leagues: tuple[str, ...]
    seasons: tuple[int, ...]
    cache_dir: Path
    db_path: Path
    offline: bool
    refresh: bool


@dataclass(frozen=True, slots=True)
class FileReport:
    """Outcome of one league season file."""

    league: str
    season: int
    status: str  # imported | missing | failed
    rows: int = 0
    skipped_rows: int = 0
    fixtures: int = 0
    odds: int = 0


def parse_seasons(text: str) -> tuple[int, ...]:
    """``"2021-2026"`` or ``"2023,2025"`` → start years."""
    try:
        if "-" in text:
            first, last = (int(part) for part in text.split("-", 1))
            return tuple(range(first, last + 1))
        return tuple(int(part) for part in text.split(",") if part.strip())
    except ValueError as exc:
        msg = f"invalid --seasons {text!r}; use 2021-2026 or 2023,2025"
        raise argparse.ArgumentTypeError(msg) from exc


def parse_leagues(text: str) -> tuple[str, ...]:
    """Comma-separated football-data division codes, validated against the catalogue."""
    codes = tuple(code.strip().upper() for code in text.split(",") if code.strip())
    unknown = [code for code in codes if code not in LEAGUES]
    if unknown or not codes:
        msg = f"unknown league code(s) {unknown}; choose from {', '.join(LEAGUES)}"
        raise argparse.ArgumentTypeError(msg)
    return codes


def parse_args(argv: Sequence[str], *, today_season: int, default_db: Path) -> ImportOptions:
    """Command-line options with the project defaults."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--leagues", type=parse_leagues, default=DEFAULT_LEAGUES)
    parser.add_argument(
        "--seasons",
        type=parse_seasons,
        default=tuple(range(DEFAULT_FIRST_SEASON, today_season + 1)),
        help="season start years, e.g. 2021-2026 (default: 2021..current)",
    )
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--db-path", type=Path, default=default_db)
    parser.add_argument("--offline", action="store_true", help="never download; use the cache")
    parser.add_argument("--refresh", action="store_true", help="re-download cached files")
    args = parser.parse_args(argv)
    return ImportOptions(
        leagues=args.leagues,
        seasons=args.seasons,
        cache_dir=args.cache_dir,
        db_path=args.db_path,
        offline=args.offline,
        refresh=args.refresh,
    )


async def ensure_schema(engine: AsyncEngine) -> None:
    """Refuse to run against a database that has not been migrated."""
    async with engine.connect() as connection:
        tables = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
    if not {"fixtures", "odds_snapshots"} <= tables:
        msg = "database schema is missing; run `uv run alembic upgrade head` first"
        raise ConfigurationError(msg)


async def obtain_file(
    client: httpx.AsyncClient | None, options: ImportOptions, league: str, season: int
) -> Path | None:
    """Local path of the season file (downloading it if allowed and needed), else None."""
    path = cache_path(options.cache_dir, league, season)
    stale = options.refresh or needs_download(path, season=season, now=datetime.now(UTC))
    if client is None or not stale:
        return path if path.is_file() else None
    try:
        published = await download_season(client, league, season, path)
    except ExternalServiceError as exc:
        if not path.is_file():
            raise
        # Better an older copy than nothing: every row in it is still a valid upsert.
        logger.warning("using_stale_cache", league=league, season=season, error=str(exc))
        return path
    return path if published else None


async def import_file(
    sessions: async_sessionmaker[AsyncSession], path: Path, league: str, season: int
) -> FileReport:
    """Parse one file and store it in a single transaction."""
    content = await asyncio.to_thread(path.read_bytes)
    parsed = await asyncio.to_thread(
        parse_season_csv, content, league=league_for(league), season=season
    )
    for skipped in parsed.skipped[:MAX_LOGGED_SKIPPED_ROWS]:
        logger.warning(
            "row_skipped", league=league, season=season, line=skipped.line, reason=skipped.reason
        )
    async with sessions() as session, session.begin():
        summary = await upsert_batch(session, parsed.batch)
    logger.info(
        "season_imported",
        league=league,
        season=season,
        rows=parsed.rows,
        skipped_rows=len(parsed.skipped),
        fixtures=summary.fixtures,
        odds=summary.odds,
        incomplete_price_groups=parsed.incomplete_price_groups,
        missing_kickoff_time=parsed.missing_kickoff_time,
    )
    return FileReport(
        league=league,
        season=season,
        status="imported",
        rows=parsed.rows,
        skipped_rows=len(parsed.skipped),
        fixtures=summary.fixtures,
        odds=summary.odds,
    )


async def import_one(
    client: httpx.AsyncClient | None,
    sessions: async_sessionmaker[AsyncSession],
    options: ImportOptions,
    league: str,
    season: int,
) -> FileReport:
    """Obtain and import one season; failures are logged and reported, never raised."""
    try:
        path = await obtain_file(client, options, league, season)
        if path is None:
            logger.warning(
                "season_file_unavailable", league=league, season=season, offline=options.offline
            )
            return FileReport(league=league, season=season, status="missing")
        return await import_file(sessions, path, league, season)
    except (AppError, SQLAlchemyError, OSError) as exc:
        # One broken file, database hiccup or disk error must not stop the other seasons;
        # the transaction of this file was rolled back, so nothing half-written remains.
        logger.error(
            "season_import_failed",
            league=league,
            season=season,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return FileReport(league=league, season=season, status="failed")


async def log_coverage(sessions: async_sessionmaker[AsyncSession]) -> None:
    """Report stored fixtures and Pinnacle closing coverage per league season."""
    async with sessions() as session:
        coverage = await season_coverage(session, source=SOURCE)
    for item in coverage:
        logger.info(
            "coverage",
            league=item.league,
            season=item.season,
            fixtures=item.fixtures,
            pinnacle_closing_pct=round(100 * item.pinnacle_closing_share, 1),
        )
    total = sum(item.fixtures for item in coverage)
    covered = sum(item.with_pinnacle_closing for item in coverage)
    logger.info(
        "coverage_total",
        fixtures=total,
        pinnacle_closing_pct=round(100 * covered / total, 1) if total else 0.0,
    )


async def run(options: ImportOptions) -> list[FileReport]:
    """Import every requested league season and log the coverage summary."""
    engine = create_engine(options.db_path)
    client = None if options.offline else create_client()
    try:
        await ensure_schema(engine)
        sessions = create_session_factory(engine)
        reports = [
            await import_one(client, sessions, options, league, season)
            for league in options.leagues
            for season in options.seasons
        ]
        await log_coverage(sessions)
    finally:
        if client is not None:
            await client.aclose()
        await engine.dispose()
    return reports


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the process exit code."""
    # Team names such as "Cádiz" must not crash logging on a legacy (cp125x) console.
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="backslashreplace")
    configure_logging(level="INFO", app_env="dev")
    try:
        settings = get_settings()
        options = parse_args(
            sys.argv[1:] if argv is None else argv,
            today_season=current_season(datetime.now(UTC).date()),
            default_db=settings.db_path,
        )
        reports = asyncio.run(run(options))
    except ConfigurationError as exc:
        logger.critical("import_aborted", reason=str(exc))
        return EXIT_CONFIGURATION_ERROR
    failed = [f"{report.league}/{report.season}" for report in reports if report.status == "failed"]
    if failed:
        logger.error("import_finished_with_failures", failed=failed)
        return EXIT_PARTIAL_FAILURE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
