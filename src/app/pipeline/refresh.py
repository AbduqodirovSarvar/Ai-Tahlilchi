"""Daily cache refresh (F4.1, plan §4 step 0), also ``python -m app.pipeline.refresh``.

Loads the next few *local* days of fixtures (a local day spans two UTC days, so the UTC days
around it are fetched too), the results of matches that finished since the last run (with
expected goals for the newest few) and the morning odds of upcoming matches. Together with
the administrator's manual refresh this is the only way provider data reaches the database
for analyses (ADR 0021).

The scheduled run is automatic-purpose, so it can never eat the manual reserve; when the
automatic budget runs out a step is skipped and reported, never half-written. Rerunning the
same day is a no-op (ledger key) unless ``force`` is set.

A refresh that had to skip a step is *not* done for the day: its key is failed (attempt
counted, exponential backoff, a few tries) so a later call - the scheduler's tick passes
``retry_only`` - completes the missing part. An unexpected error does the same instead of
leaving the key claimed until its lease runs out.

:func:`refresh_manual` is the administrator's "update the database now": the same steps on
the manual quota, with a pre-flight cost check and a minimum gap between two runs.

Exit codes of the CLI: 0 done (also "nothing to do"), 1 unexpected error, 2 configuration
error (for example no ``API_FOOTBALL_KEY``: nothing is fetched and the message says why).
"""

import asyncio
import sys
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from app.clients.cost import FetchPlan, estimate_requests
from app.db.repo import list_fixtures
from app.domain import ExternalRef, FixtureStatus, OddsKind, Sport
from app.errors import ConfigurationError
from app.logging import configure_logging, get_logger
from app.pipeline.context import PipelineContext
from app.pipeline.errors import BudgetExhaustedError, PipelineError, RefreshTooSoonError
from app.pipeline.freshness import mark_fresh
from app.pipeline.ingest import (
    ingest_fixtures,
    ingest_odds,
    ingest_results,
    provider_of,
    utc_days,
)
from app.pipeline.ledger import STAMP_DAY, LedgerKey
from app.pipeline.profile import utc_bounds
from app.pipeline.provider import AUTOMATIC, MANUAL, Purpose
from app.sports.football_leagues import seasons_between

logger = get_logger("app.pipeline.refresh")

JOB: Final = "refresh"
MANUAL_JOB: Final = "refresh.manual"
REQUIRED_STEPS: Final = frozenset({"fixtures", "odds"})
"""Without these the database is not current; results alone do not make it so."""
RESULT_LOOKBACK: Final = timedelta(days=3)
MIN_WAIT: Final = timedelta(seconds=1)
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

    @property
    def detail(self) -> str:
        """One line for the ledger and ``/holat``."""
        text = f"fixtures={self.fixtures} results={self.results} odds={self.odds}"
        return text + (" skipped=" + ",".join(self.skipped) if self.skipped else "")


@dataclass(frozen=True, slots=True, kw_only=True)
class ManualRefreshReport:
    """What an administrator's refresh stored and how many provider requests it cost."""

    started_at: datetime
    finished_at: datetime
    fixtures: int
    results: int
    odds: int
    api_requests: int
    skipped: tuple[str, ...]


async def refresh(
    ctx: PipelineContext,
    *,
    sport: Sport = Sport.FOOTBALL,
    force: bool = False,
    retry_only: bool = False,
) -> RefreshSummary:
    """Refresh fixtures, results and odds of ``sport`` (see the module docstring).

    ``retry_only``: do nothing unless today's refresh already ran and failed (the scheduler's
    tick uses it, so only the scheduled job starts a day's refresh).

    Raises:
        ProviderUnavailableError: no provider is configured for ``sport``.
    """
    summary = RefreshSummary()
    if retry_only and sport not in ctx.providers:  # database-only mode: nothing to retry
        summary.already_done = True
        return summary
    provider_of(ctx, sport)
    now = ctx.clock()
    key = LedgerKey(JOB, now.date(), 0, sport.value)
    if retry_only and not await _retryable(ctx, key):
        summary.already_done = True
        return summary
    if not await ctx.ledger.claim(key, now=now) and not force:
        summary.already_done = True
        return summary
    try:
        await _run_steps(ctx, sport, summary, AUTOMATIC)
    except Exception as error:  # the key must not stay claimed until its lease runs out
        await ctx.ledger.fail(key, now=ctx.clock(), detail=f"{type(error).__name__}: {error}"[:200])
        raise
    if summary.skipped:
        await ctx.ledger.fail(key, now=ctx.clock(), detail=summary.detail)
    else:
        await ctx.ledger.complete(key, now=ctx.clock(), detail=summary.detail)
    await _record(ctx, sport, summary)
    logger.info("refresh_done", detail=summary.detail)
    return summary


async def refresh_manual(
    ctx: PipelineContext, *, sport: Sport = Sport.FOOTBALL
) -> ManualRefreshReport:
    """The administrator's refresh: the daily steps now, on the manual quota.

    The minimum gap (``config.manual_refresh_min_interval``) is enforced with a ledger claim
    whose lease is that gap, so two taps at once or a tap after a restart cannot both run. A
    run that stored nothing releases the claim, so an outage or a refused cost check does not
    lock the button.

    Raises:
        ProviderUnavailableError: no provider is configured for ``sport``.
        RefreshTooSoonError: the previous manual refresh was less than the minimum gap ago.
        BudgetExhaustedError: the manual quota cannot cover the refresh (nothing was sent).
    """
    provider = provider_of(ctx, sport)
    started = ctx.clock()
    interval = ctx.config.manual_refresh_min_interval
    key = LedgerKey(MANUAL_JOB, STAMP_DAY, 0, sport.value)
    if not await ctx.ledger.claim(key, now=started, lease=interval):
        raise RefreshTooSoonError(await _wait_left(ctx, key, interval))
    summary = RefreshSummary()
    try:
        before = await provider.quota()
        needed = await _estimate(ctx, sport, started)
        if needed > before.available(MANUAL):
            raise BudgetExhaustedError(needed, before.available(MANUAL))
        await _run_steps(ctx, sport, summary, MANUAL)
    except BaseException:  # includes cancellation: the gap must not be spent on nothing
        await ctx.ledger.release(key)
        raise
    if summary.skipped and not (summary.fixtures or summary.results or summary.odds):
        await ctx.ledger.release(key)  # an outage that stored nothing must not lock the button
    after = await provider.quota()
    await _record(ctx, sport, summary, manual=True)
    logger.info("refresh_manual_done", detail=summary.detail)
    return ManualRefreshReport(
        started_at=started,
        finished_at=ctx.clock(),
        fixtures=summary.fixtures,
        results=summary.results,
        odds=summary.odds,
        api_requests=max(after.used - before.used, 0),
        skipped=tuple(summary.skipped),
    )


async def _wait_left(ctx: PipelineContext, key: LedgerKey, interval: timedelta) -> timedelta:
    """Time until the previous manual refresh is ``interval`` old (never zero or negative)."""
    entry = await ctx.ledger.get(key)
    if entry is None:  # released between the failed claim and now: practically free
        return MIN_WAIT
    return max(entry.claimed_at + interval - ctx.clock(), MIN_WAIT)


async def _record(
    ctx: PipelineContext, sport: Sport, summary: RefreshSummary, *, manual: bool = False
) -> None:
    """Stamp the freshness (only when fixtures and prices are in) and record the run."""
    if not REQUIRED_STEPS.intersection(summary.skipped):
        await mark_fresh(ctx, sport)
    detail = ("manual " if manual else "") + summary.detail
    await ctx.ledger.record_run(JOB, ok=summary.ok, detail=detail, now=ctx.clock())


async def _retryable(ctx: PipelineContext, key: LedgerKey) -> bool:
    """True when the key exists and may be attempted again now (failed, backoff over)."""
    return key in await ctx.ledger.due_retries(JOB, now=ctx.clock())


async def _run_steps(
    ctx: PipelineContext, sport: Sport, summary: RefreshSummary, purpose: Purpose
) -> None:
    # The matches that wait for a result are noted before the fixture fetch: it reaches back
    # to the previous UTC day (a local day spans two) and a listing that already says "finished"
    # would otherwise hide them from the results step, which also fetches their xG.
    waiting = await _pending_results(ctx, sport)
    await _step(summary, "fixtures", _fixtures(ctx, sport, summary, purpose))
    await _step(summary, "results", _results(ctx, sport, summary, purpose, waiting))
    await _step(summary, "odds", _odds(ctx, sport, summary, purpose))


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


def local_bounds(zone: ZoneInfo, now: datetime, local_days: int) -> tuple[datetime, datetime]:
    """Half-open UTC interval of the next ``local_days`` days of ``zone``, today included."""
    today = now.astimezone(zone).date()
    return utc_bounds(zone, today, today + timedelta(days=local_days - 1))


def local_utc_days(zone: ZoneInfo, now: datetime, local_days: int) -> list[date]:
    """The UTC dates that hold the next ``local_days`` calendar days of ``zone``.

    A local day crosses a UTC midnight (Tashkent is UTC+5: its 00:00-05:00 is still the
    previous UTC day), so fetching ``[today, today + N)`` by UTC date would miss matches of the
    last local day whenever the refresh runs in those early hours.
    """
    return utc_days(*local_bounds(zone, now, local_days))


async def _fixtures(
    ctx: PipelineContext, sport: Sport, summary: RefreshSummary, purpose: Purpose
) -> None:
    days = local_utc_days(ctx.config.display_zone, ctx.clock(), ctx.config.refresh_days_ahead)
    summary.fixtures = await ingest_fixtures(ctx, sport, days[0], days[-1], purpose=purpose)


async def _pending_results(ctx: PipelineContext, sport: Sport) -> list[ExternalRef]:
    """Provider fixtures that should be over by now but have no result yet."""
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
    return [item.fixture.ref for item in page.items if item.fixture.ref.source == source]


async def _results(
    ctx: PipelineContext,
    sport: Sport,
    summary: RefreshSummary,
    purpose: Purpose,
    waiting: list[ExternalRef],
) -> None:
    refs = list(dict.fromkeys([*waiting, *await _pending_results(ctx, sport)]))
    results = await ingest_results(
        ctx, sport, refs, purpose=purpose, xg_limit=ctx.config.xg_per_refresh
    )
    summary.results = len(results)


async def _upcoming(ctx: PipelineContext, sport: Sport) -> list[ExternalRef]:
    """Provider fixtures that have not started and lie inside the odds window."""
    now = ctx.clock()
    end = local_bounds(ctx.config.display_zone, now, ctx.config.odds_days_ahead)[1]
    async with ctx.sessions() as session:
        page = await list_fixtures(
            session, start=now, end=end, sport=sport, statuses=(FixtureStatus.SCHEDULED,)
        )
    source = provider_of(ctx, sport).source
    return [item.fixture.ref for item in page.items if item.fixture.ref.source == source]


async def _odds(
    ctx: PipelineContext, sport: Sport, summary: RefreshSummary, purpose: Purpose
) -> None:
    summary.odds = await ingest_odds(
        ctx, sport, await _upcoming(ctx, sport), kind=OddsKind.MORNING, purpose=purpose
    )


async def _estimate(ctx: PipelineContext, sport: Sport, now: datetime) -> int:
    """Upper bound of the provider requests a refresh would spend right now."""
    leagues = len(ctx.leagues.canonical_refs())
    zone = ctx.config.display_zone
    days = local_utc_days(zone, now, ctx.config.refresh_days_ahead)
    seasons = len(seasons_between(days[0], days[-1]))
    odds_days = len(local_utc_days(zone, now, ctx.config.odds_days_ahead))
    plan = FetchPlan(
        days=min(len(days), leagues * seasons),
        odds_league_days=leagues * odds_days,
        fixtures_by_id=len(await _pending_results(ctx, sport)),
    )
    return estimate_requests(plan)


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
