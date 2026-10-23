"""``run_real``: startup order, the handicap guard and the watchdog exit path (no network)."""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot, Dispatcher

from app import main as main_module
from app.bot.notifier import TelegramNotifier
from app.config import Settings
from app.errors import ConfigurationError
from app.ops.watchdog import EXIT_STALLED, SchedulerStalledError, Watchdog, WatchdogTiming
from app.scheduler import PipelineScheduler
from tests.bot.fakes import RecordingSession


def real_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "db_path": tmp_path / "app.db",
        "app_services": "real",
        "telegram_bot_token": "42:TEST",
        "admin_ids": "1",
        "handicap_convention": "home_line",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg, arg-type]


async def test_real_services_refuse_to_start_without_an_explicit_handicap_convention(
    tmp_path: Path,
) -> None:
    config = Settings(  # type: ignore[call-arg]
        _env_file=None,
        db_path=tmp_path / "app.db",
        app_services="real",
        telegram_bot_token="42:TEST",
        admin_ids="1",
    )
    with pytest.raises(ConfigurationError, match="HANDICAP_CONVENTION"):
        await main_module.run_bot(config)
    assert not (tmp_path / "app.db").exists()  # nothing was opened before the refusal


async def test_the_scheduler_starts_after_the_bot_is_attached_and_in_its_own_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bot = Bot(token="42:TEST", session=RecordingSession())
    seen: dict[str, Any] = {"events": []}
    real_create = main_module.create_real_services
    real_start = PipelineScheduler.start

    def spy_create(ctx: Any, notifier: Any = None, *, build: str = "") -> Any:
        seen["notifier"] = notifier
        return real_create(ctx, notifier=notifier, build=build)

    def spy_start(self: PipelineScheduler) -> None:
        seen["events"].append(("scheduler_start", seen["notifier"]._bot is not None))
        real_start(self)

    async def poll(dispatcher: Dispatcher, *_args: object) -> None:
        seen["events"].append(("poll", None))
        await dispatcher.emit_startup(bot=bot)

    monkeypatch.setattr(main_module, "create_real_services", spy_create)
    monkeypatch.setattr(PipelineScheduler, "start", spy_start)
    monkeypatch.setattr(main_module, "poll", poll)

    await main_module.run_bot(real_settings(tmp_path))

    assert seen["events"] == [("poll", None), ("scheduler_start", True)]
    assert isinstance(seen["notifier"], TelegramNotifier)
    assert (tmp_path / "jobs.db").is_file()


async def test_the_scheduler_never_starts_when_the_bot_cannot_connect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started: list[bool] = []
    monkeypatch.setattr(PipelineScheduler, "start", lambda _self: started.append(True))

    async def poll(*_args: object) -> None:
        msg = "Telegram rejected TELEGRAM_BOT_TOKEN"
        raise ConfigurationError(msg)

    monkeypatch.setattr(main_module, "poll", poll)

    with pytest.raises(ConfigurationError):
        await main_module.run_bot(real_settings(tmp_path))
    assert started == []


async def test_a_stalled_scheduler_stops_polling_and_ends_in_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bot = Bot(token="42:TEST", session=RecordingSession())
    stop_requested = asyncio.Event()
    forced: list[int] = []

    def fast_watchdog(seconds_since_tick: Any, **_ignored: Any) -> Watchdog:
        async def request_stop() -> None:
            stop_requested.set()

        return Watchdog(
            seconds_since_tick,
            stall_after=-1.0,  # any tick age counts as stale
            request_stop=request_stop,
            force_exit=lambda: forced.append(EXIT_STALLED),
            timing=WatchdogTiming(interval=0.01, force_after=30.0),
        )

    async def poll(dispatcher: Dispatcher, *_args: object) -> None:
        await dispatcher.emit_startup(bot=bot)
        await asyncio.wait_for(stop_requested.wait(), timeout=5)

    monkeypatch.setattr(main_module, "Watchdog", fast_watchdog)
    monkeypatch.setattr(main_module, "poll", poll)

    with pytest.raises(SchedulerStalledError):
        await main_module.run_bot(real_settings(tmp_path))
    assert forced == []  # the orderly stop was enough


def test_main_exits_with_the_stalled_code_and_logs_the_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def run_bot(_settings: Settings) -> None:
        msg = "stalled"
        raise SchedulerStalledError(msg)

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: real_settings(tmp_path, app_env="prod", app_version="v9.9.9", git_sha="abc123"),
    )
    monkeypatch.setattr(main_module, "run_bot", run_bot)

    assert main_module.main() == EXIT_STALLED

    output = capsys.readouterr().out
    assert '"event": "app_started"' in output
    assert '"version": "9.9.9"' in output
    assert '"sha": "abc123"' in output
    assert "scheduler_stalled_exit" in output
