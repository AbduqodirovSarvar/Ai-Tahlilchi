"""How current the stored provider data is (ADR 0021: analysis reads the database only).

Every refresh that brought both fixtures and prices stamps the ledger. An analysis whose
window reaches into the future is refused (manual) or flagged (automatic) when that stamp is
missing or older than ``config.max_quote_age``: prices that old would be skipped one by one as
stale anyway, so saying so once, with the last update time, is the clearer answer.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from app.domain import Sport
from app.pipeline.context import PipelineContext
from app.pipeline.errors import PipelineError

STAMP_JOB: Final = "db.refreshed"
FAR_FUTURE: Final = datetime.max.replace(tzinfo=UTC)


class StaleDataError(PipelineError):
    """The database was not refreshed recently enough for an analysis of the future."""

    def __init__(self, last_refresh_at: datetime | None) -> None:
        """``last_refresh_at`` is ``None`` when the database was never refreshed."""
        when = "never" if last_refresh_at is None else last_refresh_at.isoformat()
        super().__init__(f"database not refreshed (last refresh: {when})")
        self.last_refresh_at = last_refresh_at


@dataclass(frozen=True, slots=True)
class Freshness:
    """The last complete refresh and whether it is too old for the window asked about."""

    last_refresh_at: datetime | None
    stale: bool


async def mark_fresh(ctx: PipelineContext, sport: Sport = Sport.FOOTBALL) -> None:
    """Remember that fixtures and prices of ``sport`` were just refreshed."""
    await ctx.ledger.stamp(STAMP_JOB, sport.value, now=ctx.clock())


async def last_refresh_at(ctx: PipelineContext, sport: Sport = Sport.FOOTBALL) -> datetime | None:
    """When fixtures and prices of ``sport`` were last refreshed completely."""
    return await ctx.ledger.stamped_at(STAMP_JOB, sport.value)


async def freshness(
    ctx: PipelineContext, window_end: datetime, sport: Sport = Sport.FOOTBALL
) -> Freshness:
    """Freshness for an analysis whose window ends at ``window_end`` (UTC, exclusive).

    A window that lies entirely in the past (a look-back) needs no current prices, so it is
    never stale.
    """
    now = ctx.clock()
    stamped = await last_refresh_at(ctx, sport)
    if window_end <= now:
        return Freshness(stamped, stale=False)
    stale = stamped is None or now - stamped > ctx.config.max_quote_age
    return Freshness(stamped, stale=stale)


async def require_fresh(
    ctx: PipelineContext, window_end: datetime, sport: Sport = Sport.FOOTBALL
) -> Freshness:
    """:func:`freshness`, raising :class:`StaleDataError` when the data is too old.

    Raises:
        StaleDataError: the window reaches into the future and the refresh is missing or old.
    """
    result = await freshness(ctx, window_end, sport)
    if result.stale:
        raise StaleDataError(result.last_refresh_at)
    return result


async def current_freshness(ctx: PipelineContext, sport: Sport = Sport.FOOTBALL) -> Freshness:
    """Freshness for the matches still to come (no particular window)."""
    return await freshness(ctx, FAR_FUTURE, sport)
