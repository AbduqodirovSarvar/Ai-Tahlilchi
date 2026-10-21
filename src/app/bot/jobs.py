"""Background jobs of the bot: work that must outlive the update that started it.

Updates of one user are serialised by aiogram's event isolation (one lock per chat and
user), so a handler must not hold the lock for a minutes-long analysis. The handler flips
the user into a ``running`` state inside the lock, spawns the analysis here and returns;
the job takes the same lock again only for its short state changes.
"""

import asyncio
from collections.abc import Awaitable
from contextlib import AbstractAsyncContextManager

from aiogram.fsm.storage.base import BaseEventIsolation, StorageKey

from app.logging import get_logger

logger = get_logger(__name__)


class BackgroundJobs:
    """Spawns fire-and-forget tasks, keeps them referenced and logs what they leak."""

    def __init__(self, isolation: BaseEventIsolation) -> None:
        """``isolation`` is the dispatcher's event isolation (the per-user update lock)."""
        self._isolation = isolation
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def tasks(self) -> frozenset[asyncio.Task[None]]:
        """Jobs that have not finished yet (tests await them to see a flow's outcome)."""
        return frozenset(self._tasks)

    def lock(self, key: StorageKey) -> AbstractAsyncContextManager[None]:
        """The lock that serialises updates of the user/chat ``key`` (re-entrant: no)."""
        return self._isolation.lock(key)

    def spawn(self, job: Awaitable[None], *, name: str) -> asyncio.Task[None]:
        """Run ``job`` in the background; asyncio alone would keep only a weak reference."""
        task = asyncio.create_task(self._guarded(job, name), name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @staticmethod
    async def _guarded(job: Awaitable[None], name: str) -> None:
        """Last resort: a job failure is logged, never lost as "Task exception never retrieved"."""
        try:
            await job
        except Exception:
            logger.exception("background_job_failed", job=name)

    async def cancel_all(self) -> None:
        """Stop every running job (bot shutdown)."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
