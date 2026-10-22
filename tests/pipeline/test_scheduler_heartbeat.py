"""The dead-man's switch is pinged after the daily-cycle jobs, and only then."""

import httpx
import pytest
import respx

from app import scheduler as scheduler_module
from app.ops.heartbeat import Heartbeat
from app.scheduler import (
    JOB_DAILY_TICK,
    JOB_REFRESH,
    JOB_SETTLE,
    PipelineJobs,
    execute_job,
)
from tests.pipeline.harness import Env

URL = "https://hc-ping.com/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def install_with_heartbeat(env: Env) -> None:
    beat = Heartbeat(URL, attempts=1, backoff=0.0)
    scheduler_module.install(PipelineJobs(env.ctx, env.system.delivery, heartbeat=beat))


@respx.mock
async def test_a_successful_settle_pings_success(seeded_env: Env) -> None:
    ok = respx.post(URL).respond(200)
    fail = respx.post(URL + "/fail").respond(200)
    install_with_heartbeat(seeded_env)

    await execute_job(JOB_SETTLE)

    assert ok.call_count == 1
    assert not fail.called


@respx.mock
async def test_a_failing_refresh_pings_fail(seeded_env: Env) -> None:
    ok = respx.post(URL).respond(200)
    fail = respx.post(URL + "/fail").respond(200)
    install_with_heartbeat(seeded_env)

    await execute_job(JOB_REFRESH)  # no provider is configured: the job raises

    assert fail.call_count == 1
    assert not ok.called


@respx.mock
async def test_ticks_do_not_ping(seeded_env: Env) -> None:
    route = respx.route().respond(200)
    install_with_heartbeat(seeded_env)

    await execute_job(JOB_DAILY_TICK)

    assert not route.called


@respx.mock
async def test_an_unreachable_monitor_does_not_break_the_job(seeded_env: Env) -> None:
    route = respx.post(URL).mock(side_effect=httpx.ConnectError("down"))
    install_with_heartbeat(seeded_env)

    await execute_job(JOB_SETTLE)  # must return normally

    assert route.called


@respx.mock
async def test_an_unreadable_run_status_is_logged_not_raised(
    seeded_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = respx.route().respond(200)
    install_with_heartbeat(seeded_env)

    async def broken() -> dict[str, object]:
        raise RuntimeError("db gone")

    monkeypatch.setattr(seeded_env.ctx.ledger, "last_runs", broken)

    await execute_job(JOB_SETTLE)

    assert not route.called
