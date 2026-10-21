"""Tests for app.bot.jobs (background tasks of the bot)."""

import asyncio

from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import SimpleEventIsolation

from app.bot.jobs import BackgroundJobs

KEY = StorageKey(bot_id=1, chat_id=2, user_id=2)


async def test_spawned_job_runs_and_is_forgotten_when_done() -> None:
    jobs = BackgroundJobs(SimpleEventIsolation())
    done = asyncio.Event()

    async def job() -> None:
        done.set()

    task = jobs.spawn(job(), name="t")
    assert task in jobs.tasks
    await task

    assert done.is_set()
    assert jobs.tasks == frozenset()


async def test_a_failing_job_is_contained() -> None:
    jobs = BackgroundJobs(SimpleEventIsolation())

    async def job() -> None:
        msg = "boom"
        raise RuntimeError(msg)

    await jobs.spawn(job(), name="failing")  # does not raise

    assert jobs.tasks == frozenset()


async def test_cancel_all_stops_running_jobs() -> None:
    jobs = BackgroundJobs(SimpleEventIsolation())
    started = asyncio.Event()

    async def forever() -> None:
        started.set()
        await asyncio.Event().wait()

    task = jobs.spawn(forever(), name="forever")
    await started.wait()

    await jobs.cancel_all()

    assert task.cancelled()
    assert jobs.tasks == frozenset()


async def test_lock_serialises_the_same_user() -> None:
    jobs = BackgroundJobs(SimpleEventIsolation())
    order: list[str] = []

    async def worker(name: str) -> None:
        async with jobs.lock(KEY):
            order.append(f"{name}:in")
            await asyncio.sleep(0)
            order.append(f"{name}:out")

    await asyncio.gather(worker("a"), worker("b"))

    assert order == ["a:in", "a:out", "b:in", "b:out"]
