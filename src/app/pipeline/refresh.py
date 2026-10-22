"""Daily cache refresh (F4.1, plan §4 step 0), also ``python -m app.pipeline.refresh``.

Loads the next few days of fixtures, the results of matches that finished since the last
run (with expected goals for the newest few) and the morning odds of upcoming matches.
Everything is automatic-purpose, so it can never eat the manual reserve; when the automatic
budget runs out a step is skipped and reported, never half-written. Rerunning the same day is
a no-op (ledger key) unless ``force`` is set.

Exit codes of the CLI: 0 done (also "nothing to do"), 1 unexpected error, 2 configuration
error (for example no ``API_FOOTBALL_KEY``: nothing is fetched and the message says why).
"""

import asyncio
import sys
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final

from app.db.repo import list_fixtures
from app.domain import FixtureStatus, OddsKind, Sport
from app.errors import ConfigurationError
from app.logging import configure_logging, get_logger
from app.pipeline.context import PipelineContext
from app.pipeline.errors import BudgetExhaustedError, PipelineError
from app.pipeline.ingest import (
    days_between,
    ingest_fixtures,
    ingest_odds,
    ingest_results,
    provider_of,
)
from app.pipeline.ledger import LedgerKey
from app.pipeline.provider import AUTOMATIC

logger = get_logger("app.pipeline.refresh")

JOB: Final = "refresh"
RESULT_LOOKBACK: Final = timedelta(days=3)
EXIT_OK: Final = 0
EXIT_UNEXPECTED: Final = 1
EXIT_CONFIGURATION: Final = 2


@dataclass(slots=True)
class RefreshSummary:
    """What a refresh did; ``skipped`` lists steps dropped (budget, provider down)."""

    fixtures: int = 0
    results: int = 0
    odds: int = 0
    already_done: bool = False
    skipped: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when no step was skipped."""
        return not self.skipped


async def refresh(
    ctx: PipelineContext, *, sport: Sport = Sport.FOOTBALL, force: bool = False
) -> RefreshSummary:
    """Refresh fixtures, results and odds of ``sport`` (see the module docstring).

    Raises:
        ProviderUnavailableError: no provider is configured for ``sport``.
    """
    provider_of(ctx, sport)
    now = ctx.clock()
    key = LedgerKey(JOB, now.date(), 0, sport.value)
    summary = RefreshSummary()
    if not await ctx.ledger.claim(key, now=now) and not force:
        summary.already_done = True
        return summary
    await _step(summary, "fixtures", _fixtures(ctx, sport, summary))
    await _step(summary, "results", _results(ctx, sport, summary))
    await _step(summary, "odds", _odds(ctx, sport, summary))
    detail = f"fixtures={summary.fixtures} results={summary.results} odds={summary.odds}"
    if summary.skipped:
        detail += " skipped=" + ",".join(summary.skipped)
    await ctx.ledger.complete(key, now=ctx.clock(), detail=detail)
    await ctx.ledger.record_run(JOB, ok=summary.ok, detail=detail, now=ctx.clock())
    logger.info("refresh_done", detail=detail)
    return summary


async def _step(summary: RefreshSummary, name: str, work: Awaitable[None]) -> None:
    """Run one step; a spent budget or a provider failure skips it and keeps the others."""
    try:
        await work
    except BudgetExhaustedError as error:
        logger.warning("refresh_budget_exhausted", step=name, needed=error.needed)
        summary.skipped.append(name)
    except PipelineError as error:
        logger.warning("refresh_step_failed", step=name, error=str(error))
        summary.skipped.append(name)


async def _fixtures(ctx: PipelineContext, sport: Sport, summary: RefreshSummary) -> None:
    today = ctx.clock().date()
    last = today + timedelta(days=ctx.config.refresh_days_ahead - 1)
    summary.fixtures = await ingest_fixtures(ctx, sport, today, last, purpose=AUTOMATIC)


async def _results(ctx: PipelineContext, sport: Sport, summary: RefreshSummary) -> None:
    now = ctx.clock()
    async with ctx.sessions() as session:
        page = await list_fixtures(
            session,
            start=now - RESULT_LOOKBACK,
            end=now - ctx.config.match_duration,
            sport=sport,
            statuses=(FixtureStatus.SCHEDULED, FixtureStatus.LIVE),
        )
    source = provider_of(ctx, sport).source
    refs = [item.fixture.ref for item in page.items if item.fixture.ref.source == source]
    results = await ingest_results(
        ctx, sport, refs, purpose=AUTOMATIC, xg_limit=ctx.config.xg_per_refresh
    )
    summary.results = len(results)


async def _odds(ctx: PipelineContext, sport: Sport, summary: RefreshSummary) -> None:
    now = ctx.clock()
    end = now + timedelta(days=ctx.config.odds_days_ahead)
    async with ctx.sessions() as session:
        page = await list_fixtures(
            session, start=now, end=end, sport=sport, statuses=(FixtureStatus.SCHEDULED,)
        )
    source = provider_of(ctx, sport).source
    refs = [item.fixture.ref for item in page.items if item.fixture.ref.source == source]
    summary.odds = await ingest_odds(
        ctx,
        sport,
        refs,
        kind=OddsKind.MORNING,
        purpose=AUTOMATIC,
        days=days_between(now.date(), end.date()),
    )


async def _cli() -> int:
    from app.config import get_settings  # noqa: PLC0415 - only the CLI needs settings
    from app.pipeline.runtime import open_runtime  # noqa: PLC0415

    settings = get_settings()
    configure_logging(
        level=settings.log_level, app_env=settings.app_env, secret_values=settings.secret_values()
    )
    settings.require_secret("api_football_key")
    async with open_runtime(settings) as runtime:
        summary = await refresh(runtime.ctx)
    logger.info("refresh_cli_finished", already_done=summary.already_done, skipped=summary.skipped)
    return EXIT_OK


def main() -> int:
    """CLI entrypoint; returns the process exit code."""
    configure_logging(level="INFO", app_env="prod")
    try:
        return asyncio.run(_cli())
    except ConfigurationError as error:
        logger.critical("refresh_not_configured", reason=str(error))
        return EXIT_CONFIGURATION
    except Exception:
        logger.exception("refresh_failed")
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    sys.exit(main())
