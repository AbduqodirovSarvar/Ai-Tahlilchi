"""Scheduled templates (F7.4): saved queries that run by themselves on chosen weekdays.

A template with a schedule (ISO weekdays and a local time) is a small recurring digest.
:func:`run_due_templates` is called every minute by the scheduler and, per user and template:

- finds the local day the template is due for (today, or yesterday when the process was down
  over midnight and the run is still inside the catch-up window), by the user's own time zone,
  so DST and midnight need no special case: ``local_moment`` gives every local day exactly one
  instant for the time of day;
- holds it back during quiet hours (the digest rule, decision #15) and skips it when the
  catch-up window (``daily_catch_up``) has passed;
- claims ``(template, local day, user, template id)`` in the ledger *before* any network call, so
  a template runs exactly once per local day, whatever the number of ticks, restarts or
  workers, and a failure is retried with the ledger's backoff and given up on after
  :data:`app.pipeline.ledger.MAX_ATTEMPTS` tries with a throttled administrator alert;
- runs the stored query through the one analysis path (:class:`AnalysisEngine`), as an
  automatic job (the shared automatic quota), and delivers the result like a digest, labelled
  with the template's name;
- respects the loss limit (responsible gaming): when it is reached the template sends nothing
  new (the day's single notice goes out instead).

One template's failure never stops another template or another user.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final

from app.db.enums import DateMode, QueryScope, SignalSource
from app.db.repo import SavedQueryDraft, SavedQueryRecord, list_saved_queries
from app.domain import MarketType, Sport
from app.logging import get_logger
from app.pipeline.alerts import fail_key
from app.pipeline.analyze import AnalysisEngine
from app.pipeline.context import PipelineContext
from app.pipeline.delivery import DailyDelivery, Delivery
from app.pipeline.digest_store import outcome_for, remember_announcement
from app.pipeline.errors import DeliveryNotConfirmedError
from app.pipeline.ledger import LedgerKey
from app.pipeline.limit_notice import LIMIT_DETAIL, announce_loss_limit
from app.pipeline.limits import load_loss_status
from app.pipeline.profile import UserProfile, load_profile, local_moment, utc_bounds
from app.pipeline.provider import AUTOMATIC
from app.pipeline.queries import user_ids
from app.pipeline.types import AnalysisRequest

logger = get_logger(__name__)

JOB: Final = "template"
PAST_DETAIL: Final = "past_window"
"""The ledger detail of a template whose dates have all passed: nothing left to analyse."""
SATURDAY: Final = 5
SUNDAY: Final = 6
DAYS_PER_WEEK: Final = 7
HORIZON_LAST_DAY: Final = 13
"""The last day a window may reach, in days after today (14 days with today); the service
contract's ``horizon_end`` (a test compares them)."""


@dataclass(frozen=True, slots=True)
class TemplateReport:
    """What one tick did, as ``(user id, template id)`` pairs."""

    sent: tuple[tuple[int, int], ...] = ()
    held: tuple[tuple[int, int], ...] = ()
    failed: tuple[tuple[int, int], ...] = ()


def resolve_dates(query: SavedQueryDraft, today: date) -> tuple[date, date]:
    """The local dates ``query`` means on ``today`` (a relative choice follows the day)."""
    match query.date_mode:
        case DateMode.TODAY:
            return today, today
        case DateMode.TOMORROW:
            return today + timedelta(days=1), today + timedelta(days=1)
        case DateMode.WEEKEND:
            return _weekend(today)
        case DateMode.NEXT_DAYS:
            return today, today + timedelta(days=(query.days or 1) - 1)
        case DateMode.RANGE:
            return query.date_from or today, query.date_to or today


def _weekend(today: date) -> tuple[date, date]:
    """Saturday-Sunday containing or following ``today``."""
    weekday = today.weekday()
    if weekday == SUNDAY:
        return today, today
    saturday = today + timedelta(days=(SATURDAY - weekday) % DAYS_PER_WEEK)
    return saturday, saturday + timedelta(days=1)


def runnable_window(query: SavedQueryDraft, today: date) -> tuple[date, date] | None:
    """The dates a scheduled run analyses: past days and days beyond the horizon are cut off.

    A scheduled run recommends, so it never looks back; ``None`` when nothing is left.
    """
    first, last = resolve_dates(query, today)
    first = max(first, today)
    last = min(last, today + timedelta(days=HORIZON_LAST_DAY))
    return (first, last) if first <= last else None


def due_day(
    profile: UserProfile, query: SavedQueryDraft, now: datetime, catch_up: timedelta
) -> date | None:
    """The local day ``query`` is due for at ``now``, or ``None`` (not due, or missed too long).

    Today wins; yesterday counts when the run slipped over midnight (the process was down, or
    quiet hours held it) and is still within ``catch_up`` of its moment.
    """
    at = query.schedule_at
    if at is None:
        return None
    today = now.astimezone(profile.zone).date()
    for day in (today, today - timedelta(days=1)):
        if day.isoweekday() not in query.schedule_weekdays:
            continue
        moment = local_moment(profile.zone, day, at)
        if moment <= now and now - moment <= catch_up:
            return day
    return None


def template_request(
    profile: UserProfile, query: SavedQueryDraft, window: tuple[date, date]
) -> AnalysisRequest:
    """The pipeline request of a scheduled run: the template's query under the user's settings."""
    s = profile.settings
    start, end = utc_bounds(profile.zone, *window)
    ids = frozenset(query.scope_ids)
    return AnalysisRequest(
        user_id=profile.user_id,
        start=start,
        end=end,
        zone=profile.zone,
        sports=frozenset(Sport(value) for value in query.sports),
        markets=frozenset(MarketType(value) for value in query.markets),
        min_probability=query.min_prob,
        min_ev=query.min_ev,
        odds_min=s.odds_min,
        odds_max=s.odds_max,
        scope=query.scope,
        league_ids=ids if query.scope is QueryScope.LEAGUES else frozenset(),
        team_ids=ids if query.scope is QueryScope.TEAMS else frozenset(),
        fixture_ids=ids if query.scope is QueryScope.FIXTURES else frozenset(),
        default_league_ids=frozenset(s.leagues),
        watch_league_ids=profile.watch_league_ids,
        watch_team_ids=profile.watch_team_ids,
        max_signals=query.max_signals,
        per_day=query.per_day,
        sort=query.sort,
        include_no_value=query.include_no_value,
        source=SignalSource.CUSTOM if query.include_no_value else SignalSource.ANALYSIS,
        bank=profile.bank,
        unit=profile.unit,
        prematch_minutes=s.prematch_minutes,
        purpose=AUTOMATIC,
        skip_retro=True,
    )


async def run_due_templates(ctx: PipelineContext, delivery: Delivery) -> TemplateReport:
    """Run every scheduled template that is due now (see the module docstring)."""
    async with ctx.sessions() as session:
        ids = await user_ids(session)
    sent: list[tuple[int, int]] = []
    held: list[tuple[int, int]] = []
    failed: list[tuple[int, int]] = []
    for user_id in ids:
        for record, outcome in await _serve_user(ctx, delivery, user_id):
            {"sent": sent, "held": held, "failed": failed}.get(outcome, []).append(
                (user_id, record.id)
            )
    return TemplateReport(tuple(sent), tuple(held), tuple(failed))


async def _serve_user(
    ctx: PipelineContext, delivery: Delivery, user_id: int
) -> list[tuple[SavedQueryRecord, str]]:
    """The scheduled templates of one user and what became of each; a broken user is skipped."""
    try:
        async with ctx.sessions() as session:
            profile = await load_profile(session, user_id)
            records = [
                record
                for record in await list_saved_queries(session, user_id)
                if record.query.schedule_at is not None
            ]
    except Exception as error:  # e.g. a corrupt row of one user must not stop the others
        logger.warning("template_profile_failed", user_id=user_id, error=repr(error))
        await ctx.ledger.record_error(JOB, f"user {user_id} profile: {error}", now=ctx.clock())
        return []
    return [(record, await _serve(ctx, delivery, profile, record)) for record in records]


async def _serve(
    ctx: PipelineContext, delivery: Delivery, profile: UserProfile, record: SavedQueryRecord
) -> str:
    """``"sent"``, ``"held"``, ``"failed"`` or ``""`` (nothing due) for one template."""
    now = ctx.clock()
    day = due_day(profile, record.query, now, ctx.config.daily_catch_up)
    if day is None:
        return ""
    if profile.schedule.is_quiet(now):
        return "held"
    key = LedgerKey(JOB, day, profile.user_id, str(record.id))
    if not await ctx.ledger.claim(key, now=now):
        return ""
    try:
        await _run(ctx, delivery, profile, record, key)
    except Exception as error:  # one template's failure must not stop the others
        logger.warning(
            "template_failed", user_id=profile.user_id, template_id=record.id, error=repr(error)
        )
        await fail_key(
            ctx, delivery, key, error, what=f"template {record.id} of user {profile.user_id}"
        )
        return "failed"
    return "sent"


async def _run(
    ctx: PipelineContext,
    delivery: Delivery,
    profile: UserProfile,
    record: SavedQueryRecord,
    key: LedgerKey,
) -> None:
    now = ctx.clock()
    status = await load_loss_status(ctx, profile, now)
    if status.reached:  # responsible gaming: nothing new today, the single notice instead
        await announce_loss_limit(ctx, delivery, profile, status, key)
        await ctx.ledger.complete(key, now=ctx.clock(), detail=LIMIT_DETAIL)
        return
    today = profile.schedule.local_day(now)
    window = runnable_window(record.query, today)
    if window is None:
        await ctx.ledger.complete(key, now=ctx.clock(), detail=PAST_DETAIL)
        return
    outcome = await outcome_for(
        ctx, key, lambda: AnalysisEngine(ctx).run(template_request(profile, record.query, window))
    )
    message = DailyDelivery(day=today, outcome=outcome, label=record.query.name)
    reference = await delivery.daily(profile.user_id, message, key)
    if reference is None:
        raise DeliveryNotConfirmedError(f"template {record.id} of user {profile.user_id}")
    await remember_announcement(ctx, profile.user_id, outcome, reference)
    await ctx.ledger.complete(key, now=ctx.clock(), detail=f"signals={len(outcome.cards)}")
    await ctx.ledger.record_run(JOB, ok=True, detail=f"user={profile.user_id}", now=ctx.clock())
