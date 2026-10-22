"""Fix round: scheduler stop, refresh retries, delivery retries and alerts, status fields."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings, get_settings
from app.llm.service import LlmService
from app.pipeline import daily as daily_module
from app.pipeline import refresh as refresh_module
from app.pipeline.analyze import AnalysisEngine
from app.pipeline.config import StakingMode
from app.pipeline.daily import run_due_digests
from app.pipeline.delivery import DailyDelivery, Delivery, NullDelivery
from app.pipeline.ledger import MAX_ATTEMPTS, LedgerKey, backoff
from app.pipeline.refresh import EXIT_CONFIGURATION, refresh
from app.pipeline.review import LlmCandidateReviewer
from app.pipeline.runtime import open_runtime
from app.scheduler import (
    JOB_DAILY_TICK,
    JOB_REFRESH,
    PipelineJobs,
    PipelineScheduler,
    execute_job,
)
from app.services.interfaces import ApiQuota
from tests.pipeline.harness import Env, open_env
from tests.pipeline.test_flows import USER, scripted, tune

TASHKENT = ZoneInfo("Asia/Tashkent")
OTHER_USER = 2003


# ------------------------------------------------------------------------------ R5: stop
class SlowJobs(PipelineJobs):
    """Jobs that take ``seconds`` and remember whether they were allowed to finish."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.started = asyncio.Event()
        self.finished = False
        self.cancelled = False
        self.calls = 0

    async def run(self, name: str) -> None:  # noqa: ARG002 - the job name is irrelevant here
        self.calls += 1
        self.started.set()
        try:
            await asyncio.sleep(self.seconds)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.finished = True


async def fire_now(scheduler: PipelineScheduler, name: str) -> None:
    scheduler._scheduler.modify_job(name, next_run_time=datetime.now(UTC))
    scheduler._scheduler.wakeup()


async def test_stop_waits_for_a_running_job_instead_of_cancelling_it(tmp_path: Path) -> None:
    jobs = SlowJobs(seconds=0.5)
    scheduler = PipelineScheduler(jobs, db_path=tmp_path / "jobs.db", timezone=TASHKENT)
    scheduler.start()
    await fire_now(scheduler, JOB_DAILY_TICK)
    await asyncio.wait_for(jobs.started.wait(), 5)

    await scheduler.stop()

    assert jobs.finished
    assert not jobs.cancelled


async def test_after_stop_no_new_run_starts(tmp_path: Path) -> None:
    jobs = SlowJobs(seconds=0.0)
    scheduler = PipelineScheduler(jobs, db_path=tmp_path / "jobs.db", timezone=TASHKENT)
    scheduler.start()
    await scheduler.stop()

    await execute_job(JOB_DAILY_TICK)

    assert jobs.calls == 0


async def test_a_job_that_outlives_the_stop_timeout_is_cancelled(tmp_path: Path) -> None:
    jobs = SlowJobs(seconds=30)
    scheduler = PipelineScheduler(
        jobs, db_path=tmp_path / "jobs.db", timezone=TASHKENT, stop_timeout=0.2
    )
    scheduler.start()
    await fire_now(scheduler, JOB_DAILY_TICK)
    await asyncio.wait_for(jobs.started.wait(), 5)

    await asyncio.wait_for(scheduler.stop(), 5)

    assert jobs.cancelled
    assert not jobs.finished


# ------------------------------------------------------------------------------ refresh
async def test_a_refresh_with_a_skipped_step_is_not_done_for_the_day(provider_env: Env) -> None:
    ctx = provider_env.ctx
    provider = scripted(provider_env)
    provider.exhausted = True
    first = await refresh(ctx)
    assert not first.ok
    key = LedgerKey("refresh", ctx.clock().date(), 0, "football")
    entry = await ctx.ledger.get(key)
    assert entry is not None
    assert entry.status == "failed"

    soon = await refresh(ctx)  # still inside the backoff
    assert soon.already_done
    provider.exhausted = False
    provider_env.clock.advance(backoff(1))
    retried = await refresh(ctx, retry_only=True)

    assert not retried.already_done
    assert retried.ok
    assert await ctx.ledger.is_done(key)
    assert (await refresh(ctx, retry_only=True)).already_done  # now it is really done


async def test_retry_only_never_starts_the_days_refresh(provider_env: Env) -> None:
    summary = await refresh(provider_env.ctx, retry_only=True)

    assert summary.already_done
    assert scripted(provider_env).calls == []


async def test_an_unexpected_error_fails_the_refresh_key_at_once(
    provider_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_args: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(refresh_module, "_fixtures", broken)
    ctx = provider_env.ctx

    with pytest.raises(RuntimeError):
        await refresh(ctx)

    entry = await ctx.ledger.get(LedgerKey("refresh", ctx.clock().date(), 0, "football"))
    assert entry is not None
    assert entry.status == "failed"  # not "claimed" until the lease runs out
    assert entry.detail is not None
    assert "boom" in entry.detail


def test_the_cli_exits_with_the_configuration_code_for_an_invalid_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOG_LEVEL", "LOUD")
    get_settings.cache_clear()
    try:
        assert refresh_module.main() == EXIT_CONFIGURATION
    finally:
        get_settings.cache_clear()


# ------------------------------------------------------------------------------ R4: digest
class ScriptedDelivery(NullDelivery):
    """Delivery whose digest answer is scripted: raise, ``None`` (not delivered) or a reference."""

    def __init__(self) -> None:
        self.daily_script: list[object] = []
        self.daily_calls: list[DailyDelivery] = []
        self.alerts: list[tuple[str, str]] = []

    async def daily(self, user_id: int, message: DailyDelivery, key: LedgerKey) -> str | None:  # noqa: ARG002
        self.daily_calls.append(message)
        answer = self.daily_script.pop(0) if self.daily_script else "100:1"
        if isinstance(answer, Exception):
            raise answer
        return answer  # type: ignore[return-value]

    async def alert_admin(self, component: str, message: str, at: datetime) -> None:  # noqa: ARG002
        self.alerts.append((component, message))


def delivery_of(delivery: Delivery) -> ScriptedDelivery:
    assert isinstance(delivery, ScriptedDelivery)
    return delivery


async def test_a_retry_resends_the_stored_digest_without_analysing_again(
    provider_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await tune(provider_env)
    runs = []
    original = AnalysisEngine.run

    async def counting(self: AnalysisEngine, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        runs.append(1)
        return await original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(AnalysisEngine, "run", counting)
    delivery = ScriptedDelivery()
    delivery.daily_script = [ConnectionError("telegram is down"), "100:7"]

    failed = await run_due_digests(provider_env.ctx, delivery)
    provider_env.clock.advance(backoff(1))
    sent = await run_due_digests(provider_env.ctx, delivery)
    again = await run_due_digests(provider_env.ctx, delivery)

    assert failed.failed == (USER,)
    assert sent.sent == (USER,)
    assert again.sent == again.failed == ()
    assert len(runs) == 1  # one analysis (and one LLM call), two delivery attempts
    first, second = delivery.daily_calls
    assert [c.record.id for c in first.outcome.cards] == [c.record.id for c in second.outcome.cards]
    assert first.outcome.matched == second.outcome.matched


async def test_a_digest_the_notifier_did_not_confirm_stays_open(provider_env: Env) -> None:
    await tune(provider_env)
    delivery = ScriptedDelivery()
    delivery.daily_script = [None, "100:8"]  # the bot is not attached yet, then it is

    first = await run_due_digests(provider_env.ctx, delivery)
    key = LedgerKey("daily", provider_env.clock().astimezone(TASHKENT).date(), USER, "digest")
    assert first.failed == (USER,)
    assert not await provider_env.ctx.ledger.is_done(key)

    provider_env.clock.advance(backoff(1))
    second = await run_due_digests(provider_env.ctx, delivery)

    assert second.sent == (USER,)
    assert await provider_env.ctx.ledger.is_done(key)


async def test_a_digest_gives_up_after_the_attempt_limit_and_alerts_once(
    provider_env: Env,
) -> None:
    await tune(provider_env)
    delivery = ScriptedDelivery()
    delivery.daily_script = [ConnectionError("down")] * (MAX_ATTEMPTS + 3)

    for attempt in range(1, MAX_ATTEMPTS + 3):
        await run_due_digests(provider_env.ctx, delivery)
        provider_env.clock.advance(backoff(attempt))  # just past each backoff

    assert len(delivery.daily_calls) == MAX_ATTEMPTS  # then it stopped trying
    # the failures share one throttled alert; giving up has its own, never swallowed
    assert [component for component, _ in delivery.alerts] == ["daily", "daily"]
    assert "gave up" in delivery.alerts[-1][1]
    errors = await provider_env.ctx.ledger.recent_errors()
    assert len(errors) == MAX_ATTEMPTS


async def test_one_broken_profile_does_not_stop_the_other_digests(
    provider_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    await tune(provider_env)
    services = provider_env.system.services
    other = await services.settings.get_settings(OTHER_USER)
    await services.settings.update_settings(
        OTHER_USER, replace(other, min_probability=0.30, min_ev=0.0, quiet_hours=None)
    )
    real_load = daily_module.load_profile

    async def flaky(session, user_id):  # type: ignore[no-untyped-def]
        if user_id == USER:
            raise ValueError("corrupt settings row")
        return await real_load(session, user_id)

    monkeypatch.setattr(daily_module, "load_profile", flaky)
    delivery = ScriptedDelivery()

    report = await run_due_digests(provider_env.ctx, delivery)

    assert report.failed == (USER,)
    assert report.sent == (OTHER_USER,)
    assert delivery.alerts


# ------------------------------------------------------------------------------ status
async def test_status_splits_the_manual_reserve_from_what_is_spent(seeded_env: Env) -> None:
    status = await seeded_env.system.services.status.system_status()

    [quota] = status.quotas
    assert isinstance(quota, ApiQuota)
    assert quota.manual_reserve == 30
    assert quota.manual_used == 0
    assert quota.manual_left == 30
    assert status.llm is None  # no language model behind this reviewer


async def test_status_reports_the_language_model_quota(
    seeded_template: tuple[Path, object], tmp_path: Path
) -> None:
    template, world = seeded_template
    reviewer = LlmCandidateReviewer(LlmService(None))
    async with open_env(template, world, tmp_path / "llm.db", reviewer=reviewer) as env:  # type: ignore[arg-type]
        status = await env.system.services.status.system_status()

    assert status.llm is not None
    assert status.llm.requests_used == 0
    assert status.llm.requests_remaining == status.llm.requests_limit > 0
    assert status.llm.tokens_remaining == status.llm.tokens_limit > 0


# ------------------------------------------------------------------------------ staking
async def test_the_staking_mode_comes_from_the_settings(tmp_path: Path) -> None:
    kelly = Settings(_env_file=None, db_path=tmp_path / "a.db", staking_mode="kelly")  # type: ignore[call-arg]
    flat = Settings(_env_file=None, db_path=tmp_path / "b.db")  # type: ignore[call-arg]

    async with open_runtime(kelly) as runtime:
        assert runtime.ctx.config.staking is StakingMode.KELLY
    async with open_runtime(flat) as runtime:
        assert runtime.ctx.config.staking is StakingMode.FLAT


async def test_a_refresh_job_failure_alerts_through_the_throttle(provider_env: Env) -> None:
    delivery = ScriptedDelivery()
    jobs = PipelineJobs(provider_env.ctx, delivery)
    provider_env.ctx.providers.clear()  # type: ignore[attr-defined]

    for _ in range(3):
        await jobs.run(JOB_REFRESH)  # no provider: the job fails every time
        provider_env.clock.advance(timedelta(seconds=10))

    assert len(delivery.alerts) == 1
    assert [e[1] for e in await provider_env.ctx.ledger.recent_errors()] == [JOB_REFRESH] * 3
