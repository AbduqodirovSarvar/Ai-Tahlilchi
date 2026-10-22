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
- The job store lives in the application database, so the schedule survives restarts;
  ``replace_existing`` makes registering on every start idempotent.
- ``misfire_grace_time`` lets a daily job missed during downtime run when the process is back
  (settle/refresh: 6 hours); ``coalesce`` folds several missed runs into one;
  ``max_instances=1`` keeps a slow run from overlapping itself.
- Business idempotency does not rely on the scheduler: every job claims ledger keys (see
  :mod:`app.pipeline.ledger`), so a duplicate trigger changes nothing.
- A failing job is logged, recorded for ``/holat`` and reported to the administrators; it
  never takes the scheduler down. :meth:`PipelineScheduler.stop` waits for running jobs.

APScheduler pickles a job's callable and arguments, so jobs are the module-level
:func:`execute_job` with a job *name*; the live objects are looked up in the installed
:class:`PipelineJobs`.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime, time
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.logging import get_logger
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
JOB_SETTLE: Final = "settle"
JOB_REFRESH: Final = "refresh"
JOB_DAILY_TICK: Final = "daily-tick"
JOB_RECALC_TICK: Final = "recalc-tick"
JOB_NAMES: Final = (JOB_SETTLE, JOB_REFRESH, JOB_DAILY_TICK, JOB_RECALC_TICK)


class PipelineJobs:
    """The work behind each job name, with uniform error handling."""

    def __init__(self, ctx: PipelineContext, delivery: Delivery) -> None:
        """Run the jobs on ``ctx``; messages go through ``delivery``."""
        self._ctx = ctx
        self._delivery = delivery
        self._work: dict[str, Callable[[], Awaitable[object]]] = {
            JOB_SETTLE: lambda: run_settlement(ctx, delivery),
            JOB_REFRESH: lambda: refresh(ctx),
            JOB_DAILY_TICK: lambda: run_due_digests(ctx, delivery),
            JOB_RECALC_TICK: lambda: run_due_recalcs(ctx, delivery),
        }

    async def run(self, name: str) -> None:
        """Run job ``name``; failures are recorded and reported, never raised."""
        try:
            await self._work[name]()
        except Exception as error:
            now = self._ctx.clock()
            logger.exception("job_failed", job=name)
            await self._ctx.ledger.record_run(name, ok=False, detail=str(error)[:200], now=now)
            await self._ctx.ledger.record_error(name, str(error), now=now)
            await self._delivery.alert_admin(name, f"{type(error).__name__}: {error}", now)


_installed: PipelineJobs | None = None


def install(jobs: PipelineJobs) -> None:
    """Make ``jobs`` the target of scheduled triggers (call before starting the scheduler)."""
    global _installed  # noqa: PLW0603 - the scheduler can only reference module-level callables
    _installed = jobs


async def execute_job(name: str) -> None:
    """Scheduler entrypoint: run job ``name`` on the installed :class:`PipelineJobs`."""
    if _installed is None:
        logger.error("job_without_runtime", job=name)
        return
    await _installed.run(name)


class PipelineScheduler:
    """Owns the APScheduler instance and registers the pipeline jobs."""

    def __init__(self, jobs: PipelineJobs, *, db_path: Path, timezone: ZoneInfo) -> None:
        """Persist the schedule in the SQLite file ``db_path``; cron jobs use ``timezone``."""
        install(jobs)
        self._timezone = timezone
        self._scheduler = AsyncIOScheduler(
            jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{db_path.as_posix()}")},
            timezone=timezone,
            job_defaults={"coalesce": True, "max_instances": 1},
        )

    def start(self) -> None:
        """Register the jobs (idempotent) and start the scheduler on the running loop."""
        self._register()
        self._scheduler.start()
        logger.info("scheduler_started", jobs=[job.id for job in self._scheduler.get_jobs()])

    async def stop(self) -> None:
        """Stop accepting triggers and wait for running jobs to finish."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=True)
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
            self._scheduler.add_job(
                execute_job,
                trigger,
                args=[name],
                id=name,
                name=name,
                replace_existing=True,
                misfire_grace_time=grace,
            )
