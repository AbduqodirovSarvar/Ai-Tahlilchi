"""Scheduled jobs (F4.7): APScheduler with a persistent SQLAlchemy job store.

Jobs
----
- ``settle``: 08:00 every day (display time zone), results -> outcomes, profit, CLV;
- ``refresh``: 08:30 every day, fixtures/results/odds into the local cache;
- ``daily-tick`` and ``recalc-tick``: every minute. The tick *decides* what is due: each user's
  digest at their own ``send_time`` in their own time zone (changes of settings apply at once,
  no per-user job to re-register) and the claimed 24-hour/pre-match recalculation tasks.

Reliability
-----------
- The job store lives in the application database, so the schedule survives restarts. Starting
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
from app.pipeline.prematch import run_due_recalcs
from app.pipeline.refresh import refresh
from app.pipeline.settle import run_settlement

logger = get_logger(__name__)

SETTLE_AT: Final = time(8, 0)
REFRESH_AT: Final = time(8, 30)
DAILY_GRACE_SECONDS: Final = 6 * 3600
TICK_GRACE_SECONDS: Final = 60
TICK_SECONDS: Final = 60
STOP_TIMEOUT_SECONDS: Final = 60.0
"""How long :meth:`PipelineScheduler.stop` lets running jobs finish before cancelling them."""
JOB_SETTLE: Final = "settle"
JOB_REFRESH: Final = "refresh"
JOB_DAILY_TICK: Final = "daily-tick"
JOB_RECALC_TICK: Final = "recalc-tick"
JOB_NAMES: Final = (JOB_SETTLE, JOB_REFRESH, JOB_DAILY_TICK, JOB_RECALC_TICK)
HEARTBEAT_JOBS: Final = (JOB_SETTLE, JOB_REFRESH)
"""The daily cycle: after each of these ends well, the dead-man's switch is pinged."""


class PipelineJobs:
    """The work behind each job name, with uniform error handling."""

    def __init__(
        self, ctx: PipelineContext, delivery: Delivery, *, heartbeat: Heartbeat | None = None
    ) -> None:
        """Run the jobs on ``ctx``; messages go through ``delivery``.

        ``heartbeat`` (the dead-man's switch) is pinged after each daily-cycle job.
        """
        self._ctx = ctx
        self._delivery = delivery
        self._heartbeat = heartbeat
        self._work: dict[str, Callable[[], Awaitable[object]]] = {
            JOB_SETTLE: lambda: run_settlement(ctx, delivery),
            JOB_REFRESH: lambda: refresh(ctx),
            JOB_DAILY_TICK: lambda: run_due_digests(ctx, delivery),
            JOB_RECALC_TICK: self._recalc_tick,
        }

    async def _recalc_tick(self) -> None:
        """Due 24 h / pre-match recalculations, and the retry of a refresh that was incomplete."""
        await run_due_recalcs(self._ctx, self._delivery)
        await refresh(self._ctx, retry_only=True)

    async def run(self, name: str) -> None:
        """Run job ``name``; failures are recorded and reported, never raised."""
        try:
            await self._work[name]()
        except Exception as error:
            now = self._ctx.clock()
            logger.exception("job_failed", job=name)
            await self._ctx.ledger.record_run(name, ok=False, detail=str(error)[:200], now=now)
            await self._ctx.ledger.record_error(name, str(error), now=now)
            await alert_throttled(
                self._ctx, self._delivery, name, f"{type(error).__name__}: {error}"
            )
            await self._ping(name, PingKind.FAIL, f"{name}: {type(error).__name__}")
        else:
            await self._ping_after_run(name)

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
            await self._ping(name, PingKind.FAIL, f"{name}: {recorded.detail or 'failed'}")
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


def install(jobs: PipelineJobs) -> None:
    """Make ``jobs`` the target of scheduled triggers (call before starting the scheduler)."""
    global _installed, _tracker  # noqa: PLW0603 - the scheduler can only reference module-level callables
    _installed = jobs
    _tracker = JobTracker()


async def execute_job(name: str) -> None:
    """Scheduler entrypoint: run job ``name`` on the installed :class:`PipelineJobs`."""
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
        self._scheduler.resume()
        logger.info("scheduler_started", jobs=[job.id for job in self._scheduler.get_jobs()])

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
            (JOB_DAILY_TICK, IntervalTrigger(seconds=TICK_SECONDS), TICK_GRACE_SECONDS),
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
