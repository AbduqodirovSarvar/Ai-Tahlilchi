"""Scheduled jobs (F4.7): APScheduler with a persistent SQLAlchemy job store.

Jobs
----
- ``settle``: 08:00 every day (display time zone), results -> outcomes, profit, CLV;
- ``refresh``: 08:30 every day, fixtures/results/odds into the local cache;
- ``learning``: 09:00 every Monday, segment review and calibration (F8.5, F8.6);
- ``feedback``: 08:45 every day, after the results are in: post-mortems of the new losses and the
  lessons built from them (F8.3, F8.4); ``lessons``: 09:30 every Monday, the lesson maintenance
  (confirmation and expiry sweep);
- ``retention``: 04:00 every Sunday, prunes old intermediate price snapshots (the quietest hour,
  before anyone's morning digest);
- ``daily-tick``, ``template-tick`` and ``recalc-tick``: every minute. The tick *decides* what is
  due: each user's digest at their own ``send_time`` in their own time zone, each scheduled
  template at its weekdays and local time (changes of settings apply at once, no per-user job
  to re-register) and the claimed 24-hour/pre-match recalculation tasks.

Reliability
-----------
- The job store lives in its own SQLite file (``jobs.db`` next to the application database, see
  :attr:`app.config.Settings.jobs_db_path`), so the schedule survives restarts. It holds pickles
  and is rebuilt on start, which is why it is neither backed up nor restored with the data.
  Starting
  registers a job only when it is absent (or its trigger changed): re-adding with
  ``replace_existing`` would recompute ``next_run_time`` and silently drop a run missed while
  the process was down.
- ``misfire_grace_time`` lets a daily job missed during downtime run when the process is back
  (settle/refresh: 6 hours); ``coalesce`` folds several missed runs into one;
  ``max_instances=1`` keeps a slow run from overlapping itself.
- Business idempotency does not rely on the scheduler: every job claims ledger keys (see
  :mod:`app.pipeline.ledger`), so a duplicate trigger changes nothing.
- A failing job is logged, recorded for ``/holat`` and reported to the administrators; it
  never takes the scheduler down. :meth:`PipelineScheduler.stop` stops the ticking first, then
  waits (up to a timeout) for the jobs that are running - APScheduler shutdown would cancel
  them mid-flight - and only then shuts down.

APScheduler pickles a job's callable and arguments, so jobs are the module-level
:func:`execute_job` with a job *name*; the live objects are looked up in the installed
:class:`PipelineJobs`.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, time
from pathlib import Path
from time import monotonic
from typing import Final
from zoneinfo import ZoneInfo

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.logging import get_logger
from app.ops.heartbeat import Heartbeat, PingKind
from app.pipeline.alerts import alert_throttled
from app.pipeline.context import PipelineContext
from app.pipeline.daily import run_due_digests
from app.pipeline.delivery import Delivery
from app.pipeline.learning import run_learning
from app.pipeline.learning_digest import announce_learning
from app.pipeline.lessons import run_lessons
from app.pipeline.postmortem import run_postmortems
from app.pipeline.prematch import run_due_recalcs
from app.pipeline.refresh import refresh
from app.pipeline.retention import run_retention
from app.pipeline.settle import run_settlement
from app.pipeline.templates import run_due_templates

logger = get_logger(__name__)

SETTLE_AT: Final = time(8, 0)
LEARNING_AT: Final = time(9, 0)
LEARNING_WEEKDAY: Final = "mon"
"""The weekly learning review runs after the morning settlement, on Mondays."""
FEEDBACK_AT: Final = time(8, 45)
"""Post-mortems run after the settlement (08:00) and the refresh (08:30) of the same morning."""
LESSONS_AT: Final = time(9, 30)
RETENTION_AT: Final = time(4, 0)
RETENTION_WEEKDAY: Final = "sun"
REFRESH_AT: Final = time(8, 30)
DAILY_GRACE_SECONDS: Final = 6 * 3600
TICK_GRACE_SECONDS: Final = 60
TICK_SECONDS: Final = 60
FAILURE_BACKOFF_SECONDS: Final = 5.0
"""Pause after a failure that could not even be recorded (the database is locked): the next
tick finds it settled instead of hammering it."""
STOP_TIMEOUT_SECONDS: Final = 60.0
"""How long :meth:`PipelineScheduler.stop` lets running jobs finish before cancelling them."""
JOB_SETTLE: Final = "settle"
JOB_REFRESH: Final = "refresh"
JOB_DAILY_TICK: Final = "daily-tick"
JOB_RECALC_TICK: Final = "recalc-tick"
JOB_LEARNING: Final = "learning"
JOB_TEMPLATE_TICK: Final = "template-tick"
JOB_FEEDBACK: Final = "feedback"
JOB_LESSONS: Final = "lessons"
JOB_RETENTION: Final = "retention"
JOB_NAMES: Final = (
    JOB_SETTLE,
    JOB_REFRESH,
    JOB_DAILY_TICK,
    JOB_TEMPLATE_TICK,
    JOB_RECALC_TICK,
    JOB_LEARNING,
    JOB_FEEDBACK,
    JOB_LESSONS,
    JOB_RETENTION,
)
HEARTBEAT_JOBS: Final = (JOB_SETTLE, JOB_REFRESH)
"""The daily cycle: after each of these ends well, the dead-man's switch is pinged."""


class PipelineJobs:
    """The work behind each job name, with uniform error handling."""

    def __init__(
        self,
        ctx: PipelineContext,
        delivery: Delivery,
        *,
        heartbeat: Heartbeat | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Run the jobs on ``ctx``; messages go through ``delivery``.

        ``heartbeat`` (the dead-man's switch) is pinged after each daily-cycle job; ``sleep``
        is injectable so tests do not wait out :data:`FAILURE_BACKOFF_SECONDS`.
        """
        self._ctx = ctx
        self._delivery = delivery
        self._heartbeat = heartbeat
        self._sleep = sleep
        self._work: dict[str, Callable[[], Awaitable[object]]] = {
            JOB_SETTLE: lambda: run_settlement(ctx, delivery),
            JOB_REFRESH: lambda: refresh(ctx),
            JOB_DAILY_TICK: lambda: run_due_digests(ctx, delivery),
            JOB_RECALC_TICK: self._recalc_tick,
            JOB_LEARNING: self._learning,
            JOB_TEMPLATE_TICK: lambda: run_due_templates(ctx, delivery),
            JOB_FEEDBACK: self._feedback,
            JOB_LESSONS: lambda: run_lessons(ctx),
            JOB_RETENTION: self._retention,
        }

    async def _recalc_tick(self) -> None:
        """Due 24 h / pre-match recalculations, and the retry of a refresh that was incomplete."""
        await run_due_recalcs(self._ctx, self._delivery)
        await refresh(self._ctx, retry_only=True)

    async def _learning(self) -> None:
        """The weekly review, recorded for ``/holat``, then the "what the model learned" message.

        The message is built from the journal, so repeating this call after a crash between the
        review and the messages still tells the users that were missed.
        """
        report = await run_learning(self._ctx)
        if not report.skipped:
            detail = f"signals={report.signals} changes={len(report.changes)}"
            await self._ctx.ledger.record_run(
                JOB_LEARNING, ok=True, detail=detail, now=self._ctx.clock()
            )
        await announce_learning(self._ctx, self._delivery)

    async def _retention(self) -> None:
        """The weekly pruning of old price snapshots, recorded for ``/holat``."""
        report = await run_retention(self._ctx)
        if not report.skipped:
            await self._ctx.ledger.record_run(
                JOB_RETENTION, ok=True, detail=f"deleted={report.deleted}", now=self._ctx.clock()
            )

    async def _feedback(self) -> None:
        """Explain the new losses, then rebuild the lessons if there is new evidence."""
        report = await run_postmortems(self._ctx)
        if report.explained or report.copied:
            await run_lessons(self._ctx)

    async def run(self, name: str) -> None:
        """Run job ``name``; failures are recorded and reported, never raised."""
        try:
            await self._work[name]()
        except Exception as error:
            logger.exception("job_failed", job=name)
            await self._record_failure(name, error)
            await self._ping(name, PingKind.FAIL, f"{name}: {type(error).__name__}")
        else:
            await self._ping_after_run(name)

    async def _record_failure(self, name: str, error: Exception) -> None:
        """Record a failed run for ``/holat`` and alert; never raises (T6-F-2).

        The record goes to the same database that may be the reason for the failure (locked):
        then the failure is only logged (``job_failed_unrecorded``), the job backs off briefly
        and the next tick retries, so the scheduler is never taken down by its own bookkeeping.
        """
        try:
            now = self._ctx.clock()
            await self._ctx.ledger.record_run(name, ok=False, detail=str(error)[:200], now=now)
            await self._ctx.ledger.record_error(name, str(error), now=now)
            await alert_throttled(
                self._ctx, self._delivery, name, f"{type(error).__name__}: {error}"
            )
        except Exception as recording_error:
            logger.error(
                "job_failed_unrecorded",
                job=name,
                error=repr(error),
                recording_error=repr(recording_error),
            )
            await self._sleep(FAILURE_BACKOFF_SECONDS)

    async def _ping_after_run(self, name: str) -> None:
        """Ping success when the run ended well, ``/fail`` when it recorded a failure."""
        if self._heartbeat is None or name not in HEARTBEAT_JOBS:
            return
        try:
            recorded = (await self._ctx.ledger.last_runs()).get(name)
        except Exception:
            # Monitoring must never break the job it watches; the missing ping is the signal.
            logger.exception("heartbeat_status_unreadable", job=name)
            return
        if recorded is not None and not recorded.ok:
            await self._ping(name, PingKind.FAIL, f"{name}: failed")
        else:
            await self._ping(name, PingKind.SUCCESS, f"{name} ok")

    async def _ping(self, name: str, kind: PingKind, detail: str) -> None:
        """Heartbeat for a daily-cycle job (``Heartbeat.ping`` never raises)."""
        if self._heartbeat is not None and name in HEARTBEAT_JOBS:
            await self._heartbeat.ping(kind, detail)


class JobTracker:
    """Knows which job runs are in flight, so a stop can wait for them.

    A run is started as its own task and awaited through ``shield``: when APScheduler cancels
    the waiting coroutine on shutdown the job itself keeps going, and :meth:`drain` decides
    how long it may.
    """

    def __init__(self) -> None:
        """Start open: runs are accepted until :meth:`close`."""
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    def close(self) -> None:
        """Refuse new runs (the ticking has stopped)."""
        self._closed = True

    @property
    def running(self) -> int:
        """Runs currently in flight."""
        return len(self._tasks)

    async def run(self, name: str, jobs: PipelineJobs) -> None:
        """Run job ``name`` unless closed; the run is tracked until it ends."""
        if self._closed:
            logger.info("job_refused_while_stopping", job=name)
            return
        task = asyncio.ensure_future(jobs.run(name))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        await asyncio.shield(task)

    async def drain(self, patience: float) -> int:
        """Wait up to ``patience`` seconds for the runs; returns how many had to be cancelled."""
        pending = set(self._tasks)
        if not pending:
            return 0
        _, unfinished = await asyncio.wait(pending, timeout=patience)
        for task in unfinished:
            task.cancel()
        if unfinished:
            await asyncio.gather(*unfinished, return_exceptions=True)
        return len(unfinished)


_installed: PipelineJobs | None = None
_tracker = JobTracker()
_last_tick_at: float | None = None
"""Monotonic time of the last ``daily-tick`` start: the pulse the watchdog listens to."""


def install(jobs: PipelineJobs) -> None:
    """Make ``jobs`` the target of scheduled triggers (call before starting the scheduler)."""
    global _installed, _tracker, _last_tick_at  # noqa: PLW0603 - the scheduler can only reference module-level callables
    _installed = jobs
    _tracker = JobTracker()
    _last_tick_at = None


async def execute_job(name: str) -> None:
    """Scheduler entrypoint: run job ``name`` on the installed :class:`PipelineJobs`."""
    global _last_tick_at  # noqa: PLW0603 - module-level state is the only thing a pickled job can reach
    if name == JOB_DAILY_TICK:
        _last_tick_at = monotonic()
    if _installed is None:
        logger.error("job_without_runtime", job=name)
        return
    await _tracker.run(name, _installed)


class PipelineScheduler:
    """Owns the APScheduler instance and registers the pipeline jobs."""

    def __init__(
        self,
        jobs: PipelineJobs,
        *,
        db_path: Path,
        timezone: ZoneInfo,
        stop_timeout: float = STOP_TIMEOUT_SECONDS,
    ) -> None:
        """Persist the schedule in the SQLite file ``db_path``; cron jobs use ``timezone``."""
        self._started_at: float | None = None
        install(jobs)
        self._tracker = _tracker
        self._stop_timeout = stop_timeout
        self._timezone = timezone
        self._scheduler = AsyncIOScheduler(
            jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{db_path.as_posix()}")},
            timezone=timezone,
            job_defaults={"coalesce": True, "max_instances": 1},
        )

    def start(self) -> None:
        """Register the jobs (idempotent) and start the scheduler on the running loop.

        Starts paused so the persisted jobs can be inspected before anything fires; once
        resumed, a run missed while the process was down (inside its grace time) fires once.
        """
        self._scheduler.start(paused=True)
        self._register()
        self._started_at = monotonic()
        self._scheduler.resume()
        logger.info("scheduler_started", jobs=[job.id for job in self._scheduler.get_jobs()])

    def seconds_since_tick(self) -> float:
        """Seconds since the last ``daily-tick`` ran (since start before the first one)."""
        if self._started_at is None:
            return 0.0
        latest = max(self._started_at, _last_tick_at or 0.0)
        return monotonic() - latest

    async def stop(self) -> None:
        """Stop ticking, let the running jobs finish (up to a timeout), then shut down."""
        if not self._scheduler.running:
            logger.info("scheduler_stopped")
            return
        self._scheduler.pause()
        self._tracker.close()
        cancelled = await self._tracker.drain(self._stop_timeout)
        if cancelled:
            logger.warning("jobs_cancelled_on_stop", count=cancelled)
        self._scheduler.shutdown(wait=False)
        logger.info("scheduler_stopped")

    def next_runs(self) -> dict[str, datetime | None]:
        """Next trigger time of every registered job, by job id."""
        return {str(job.id): job.next_run_time for job in self._scheduler.get_jobs()}

    def _register(self) -> None:
        for name, trigger, grace in (
            (
                JOB_SETTLE,
                CronTrigger(hour=SETTLE_AT.hour, minute=SETTLE_AT.minute, timezone=self._timezone),
                DAILY_GRACE_SECONDS,
            ),
            (
                JOB_REFRESH,
                CronTrigger(
                    hour=REFRESH_AT.hour, minute=REFRESH_AT.minute, timezone=self._timezone
                ),
                DAILY_GRACE_SECONDS,
            ),
            (
                JOB_LEARNING,
                CronTrigger(
                    day_of_week=LEARNING_WEEKDAY,
                    hour=LEARNING_AT.hour,
                    minute=LEARNING_AT.minute,
                    timezone=self._timezone,
                ),
                DAILY_GRACE_SECONDS,
            ),
            (
                JOB_FEEDBACK,
                CronTrigger(
                    hour=FEEDBACK_AT.hour, minute=FEEDBACK_AT.minute, timezone=self._timezone
                ),
                DAILY_GRACE_SECONDS,
            ),
            (
                JOB_LESSONS,
                CronTrigger(
                    day_of_week=LEARNING_WEEKDAY,
                    hour=LESSONS_AT.hour,
                    minute=LESSONS_AT.minute,
                    timezone=self._timezone,
                ),
                DAILY_GRACE_SECONDS,
            ),
            (
                JOB_RETENTION,
                CronTrigger(
                    day_of_week=RETENTION_WEEKDAY,
                    hour=RETENTION_AT.hour,
                    minute=RETENTION_AT.minute,
                    timezone=self._timezone,
                ),
                DAILY_GRACE_SECONDS,
            ),
            (JOB_DAILY_TICK, IntervalTrigger(seconds=TICK_SECONDS), TICK_GRACE_SECONDS),
            (JOB_TEMPLATE_TICK, IntervalTrigger(seconds=TICK_SECONDS), TICK_GRACE_SECONDS),
            (JOB_RECALC_TICK, IntervalTrigger(seconds=TICK_SECONDS), TICK_GRACE_SECONDS),
        ):
            self._ensure_job(name, trigger, grace)

    def _ensure_job(self, name: str, trigger: BaseTrigger, grace: int) -> None:
        """Add job ``name`` or bring a persisted one up to date without moving its next run."""
        job = self._scheduler.get_job(name)
        if job is None:
            self._scheduler.add_job(
                execute_job,
                trigger,
                args=[name],
                id=name,
                name=name,
                misfire_grace_time=grace,
            )
            return
        if repr(job.trigger) != repr(trigger):
            self._scheduler.reschedule_job(name, trigger=trigger)
        self._scheduler.modify_job(name, args=[name], misfire_grace_time=grace, coalesce=True)
