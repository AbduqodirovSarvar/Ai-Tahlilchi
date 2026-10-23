"""In-process watchdog: a scheduler that stopped ticking takes the process down.

Docker's HEALTHCHECK only marks a container unhealthy; it does not restart it. A scheduler
that died or hung while the bot keeps polling would therefore leave the daily cycle silently
broken. The scheduler's ``daily-tick`` job fires every minute, so if no tick has run for
``stall_after`` seconds the watchdog logs ``watchdog_stalled`` and asks the application to
stop; the process then exits non-zero and ``restart: unless-stopped`` brings it back. If the
orderly stop does not complete within ``force_after`` seconds the process is killed outright.

The caller starts the watchdog only once the scheduler is running, and the staleness clock
starts at that moment, so startup and migrations can never trigger it.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from app.errors import AppError
from app.logging import get_logger

logger = get_logger(__name__)

CHECK_INTERVAL_SECONDS: Final = 30.0
FORCE_EXIT_AFTER_SECONDS: Final = 150.0
"""Longer than the scheduler's 60 s job drain, shorter than Docker's 75 s + restart delay."""
EXIT_STALLED: Final = 3
"""Process exit code after a stall (distinct from 1 unexpected error, 2 configuration error)."""


@dataclass(frozen=True, slots=True)
class WatchdogTiming:
    """How often to look and how long an orderly stop may take."""

    interval: float = CHECK_INTERVAL_SECONDS
    force_after: float = FORCE_EXIT_AFTER_SECONDS


class SchedulerStalledError(AppError):
    """The scheduler stopped ticking; the process must restart."""


class Watchdog:
    """Watches ``seconds_since_tick`` and reacts once when it exceeds ``stall_after``."""

    def __init__(
        self,
        seconds_since_tick: Callable[[], float],
        *,
        stall_after: float,
        request_stop: Callable[[], Awaitable[None]],
        force_exit: Callable[[], None],
        timing: WatchdogTiming = WatchdogTiming(),  # noqa: B008 - frozen, shared on purpose
    ) -> None:
        """``request_stop`` starts the orderly shutdown; ``force_exit`` is the last resort."""
        self._seconds_since_tick = seconds_since_tick
        self._stall_after = stall_after
        self._request_stop = request_stop
        self._force_exit = force_exit
        self._timing = timing
        self.stalled = False

    async def run(self) -> None:
        """Check until a stall is found (then stop the application) or the task is cancelled."""
        while True:
            await asyncio.sleep(self._timing.interval)
            stale = self._seconds_since_tick()
            if stale > self._stall_after:
                await self._react(stale)
                return

    async def _react(self, stale: float) -> None:
        self.stalled = True
        logger.critical(
            "watchdog_stalled", seconds_since_tick=round(stale), limit_seconds=self._stall_after
        )
        try:
            await self._request_stop()
        except RuntimeError:
            # Polling is not running (yet): nothing orderly to stop, so go straight to the kill.
            logger.warning("watchdog_stop_unavailable")
            self._force_exit()
            return
        await asyncio.sleep(self._timing.force_after)
        logger.critical("watchdog_force_exit", after_seconds=self._timing.force_after)
        self._force_exit()
