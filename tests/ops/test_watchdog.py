"""Watchdog: reacts once to a stale scheduler tick, never to a healthy one."""

import asyncio

import pytest

from app.ops.watchdog import Watchdog, WatchdogTiming

FAST = WatchdogTiming(interval=0.01, force_after=0.05)


class Probe:
    """Collects what the watchdog asked for."""

    def __init__(self, *, stop_raises: bool = False) -> None:
        self.stops = 0
        self.forced = 0
        self._stop_raises = stop_raises

    async def request_stop(self) -> None:
        self.stops += 1
        if self._stop_raises:
            msg = "polling is not running"
            raise RuntimeError(msg)

    def force_exit(self) -> None:
        self.forced += 1


def watchdog(stale: float, probe: Probe, limit: float = 600.0) -> Watchdog:
    return Watchdog(
        lambda: stale,
        stall_after=limit,
        request_stop=probe.request_stop,
        force_exit=probe.force_exit,
        timing=FAST,
    )


async def stop_task(task: "asyncio.Task[None]") -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_fresh_tick_never_triggers_it() -> None:
    probe = Probe()
    dog = watchdog(5.0, probe)
    task = asyncio.create_task(dog.run())
    await asyncio.sleep(0.1)
    await stop_task(task)
    assert (probe.stops, probe.forced, dog.stalled) == (0, 0, False)


async def test_a_stale_tick_logs_and_asks_for_an_orderly_stop(
    capsys: pytest.CaptureFixture[str],
) -> None:
    probe = Probe()
    dog = watchdog(601.0, probe)

    task = asyncio.create_task(dog.run())
    await asyncio.sleep(0.03)  # past the first check, before the force deadline

    assert dog.stalled
    assert probe.stops == 1
    assert probe.forced == 0
    await stop_task(task)  # the application shut down in time
    assert probe.forced == 0
    assert "watchdog_stalled" in capsys.readouterr().out


async def test_the_process_is_killed_when_the_orderly_stop_hangs() -> None:
    probe = Probe()
    await watchdog(9999.0, probe).run()
    assert (probe.stops, probe.forced) == (1, 1)


async def test_without_running_polling_it_goes_straight_to_the_kill() -> None:
    probe = Probe(stop_raises=True)
    await watchdog(9999.0, probe).run()
    assert (probe.stops, probe.forced) == (1, 1)


async def test_the_limit_is_exclusive() -> None:
    probe = Probe()
    dog = watchdog(600.0, probe, limit=600.0)
    task = asyncio.create_task(dog.run())
    await asyncio.sleep(0.05)
    await stop_task(task)
    assert not dog.stalled
