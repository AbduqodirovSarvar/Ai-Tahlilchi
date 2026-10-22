"""Fetching provider data and storing it (the only place the pipeline writes provider data).

Every function follows the ADR 0007 rule for I/O: *read → network → short write → commit*.
No database session is open while a provider request is in flight, and each write is its own
committed transaction, so a crash or a quota error between steps loses nothing that was
already fetched. Writes are idempotent upserts.

Data coverage (which days of fixtures and odds were fetched, and when) is remembered in the
ledger, so neither a restart nor a second user's request repeats a fetch and cost estimates
know what is already cached.
"""

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Final

from app.db.repo import upsert_live
from app.domain import ExternalRef, FixtureResult, OddsKind, Sport
from app.logging import get_logger
from app.pipeline.context import PipelineContext
from app.pipeline.errors import PipelineError, ProviderUnavailableError
from app.pipeline.leagues import HISTORY_SOURCE
from app.pipeline.provider import Purpose, SportProvider
from app.pipeline.queries import league_teams
from app.sports.base import FixtureQuery

logger = get_logger(__name__)

FIXTURES_COVERAGE: Final = "cover.fixtures"
ODDS_COVERAGE: Final = "cover.odds"


def provider_of(ctx: PipelineContext, sport: Sport) -> SportProvider:
    """The provider of ``sport``.

    Raises:
        ProviderUnavailableError: none is configured (database-only mode).
    """
    provider = ctx.providers.get(sport)
    if provider is None:
        raise ProviderUnavailableError(str(sport), "no data provider is configured")
    return provider


async def is_covered(
    ctx: PipelineContext, job: str, day: date, *, ttl: timedelta, sport: Sport = Sport.FOOTBALL
) -> bool:
    """True when ``day`` was fetched for ``job`` less than ``ttl`` ago."""
    stamped = await ctx.ledger.stamped_at(job, f"{sport.value}:{day.isoformat()}")
    return stamped is not None and ctx.clock() - stamped < ttl


async def mark_covered(
    ctx: PipelineContext, job: str, days: Sequence[date], *, sport: Sport = Sport.FOOTBALL
) -> None:
    """Remember that ``days`` were fetched for ``job`` just now."""
    now = ctx.clock()
    for day in days:
        await ctx.ledger.stamp(job, f"{sport.value}:{day.isoformat()}", now=now)


def days_between(start: date, end: date) -> list[date]:
    """Every date from ``start`` to ``end`` inclusive."""
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


async def ingest_fixtures(
    ctx: PipelineContext, sport: Sport, date_from: date, date_to: date, *, purpose: Purpose
) -> int:
    """Fetch the fixtures of the UTC days ``[date_from, date_to]`` and store them.

    Provider teams are rewritten to their history identity first, so a club has one row.
    Returns the number of fixtures written.
    """
    adapter = provider_of(ctx, sport).adapter(purpose)
    fetched = await adapter.fetch_fixtures(FixtureQuery(date_from=date_from, date_to=date_to))
    async with ctx.sessions() as session:
        history = {
            league: await league_teams(session, [league], source=HISTORY_SOURCE)
            for league in ctx.leagues.canonical_refs()
        }
    fixtures, summaries = ctx.leagues.canonicalize(fetched, history)
    for summary in summaries:
        if summary.unmatched or summary.ambiguous:
            logger.warning(
                "team_matching_incomplete",
                league=str(summary.league),
                matched=summary.matched,
                ambiguous=summary.ambiguous,
                unmatched=summary.unmatched,
            )
    async with ctx.sessions.begin() as session:
        await upsert_live(session, fixtures=fixtures)
    await mark_covered(ctx, FIXTURES_COVERAGE, days_between(date_from, date_to), sport=sport)
    return len(fixtures)


async def ingest_odds(
    ctx: PipelineContext,
    sport: Sport,
    fixtures: Sequence[ExternalRef],
    *,
    kind: OddsKind,
    purpose: Purpose,
    days: Sequence[date] = (),
) -> int:
    """Fetch current prices of ``fixtures`` as ``kind`` and store them; returns rows written.

    ``days`` are the UTC days to record as covered afterwards.
    """
    if not fixtures:
        return 0
    adapter = provider_of(ctx, sport).adapter(purpose)
    snapshots = await adapter.fetch_odds(list(fixtures), kind=kind)
    async with ctx.sessions.begin() as session:
        await upsert_live(session, odds=snapshots)
    if days:
        await mark_covered(ctx, ODDS_COVERAGE, days, sport=sport)
    return len(snapshots)


async def ingest_results(
    ctx: PipelineContext,
    sport: Sport,
    fixtures: Sequence[ExternalRef],
    *,
    purpose: Purpose,
    xg_limit: int = 0,
) -> list[FixtureResult]:
    """Fetch the results of ``fixtures`` that are over and store them.

    Expected goals are added for at most ``xg_limit`` newly finished fixtures (one request
    each). Returns the stored results.
    """
    if not fixtures:
        return []
    provider = provider_of(ctx, sport)
    results = await provider.adapter(purpose).fetch_results(list(fixtures))
    results = await _with_xg(provider, results, purpose, xg_limit)
    async with ctx.sessions.begin() as session:
        await upsert_live(session, results=results)
    return results


async def _with_xg(
    provider: SportProvider,
    results: list[FixtureResult],
    purpose: Purpose,
    limit: int,
) -> list[FixtureResult]:
    """``results`` with xG for the first ``limit`` finished ones; a provider failure keeps them."""
    if limit <= 0:
        return results
    head, tail = results[:limit], results[limit:]
    try:
        return [*await provider.enrich_results(head, purpose=purpose), *tail]
    except PipelineError as error:
        logger.warning("xg_unavailable", error=str(error))
        return results


def utc_days(start: datetime, end: datetime) -> list[date]:
    """The UTC dates touched by the half-open interval ``[start, end)``."""
    return days_between(start.date(), (end - timedelta(microseconds=1)).date())
