"""Tests for app.main (no network: Telegram calls and polling are replaced by mocks)."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.methods import GetMe
from aiogram.types import User

from app import main as main_module
from app.bot.messaging import FloodControlMiddleware
from app.config import Settings, get_settings
from app.errors import ConfigurationError
from app.services.interfaces import Services

TOKEN = "42:TEST-TOKEN-VALUE"


@pytest.fixture
def start_polling(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock()
    monkeypatch.setattr(Dispatcher, "start_polling", mock)
    return mock


@pytest.fixture
def get_me(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock(return_value=User(id=42, is_bot=True, first_name="Bot"))
    monkeypatch.setattr(Bot, "get_me", mock)
    return mock


@pytest.fixture
def session_close(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock()
    monkeypatch.setattr("aiogram.client.session.aiohttp.AiohttpSession.close", mock)
    return mock


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"telegram_bot_token": TOKEN, "admin_ids": "1"}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def json_records(output: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.strip().splitlines()]


async def test_run_bot_verifies_token_then_polls(
    start_polling: AsyncMock, get_me: AsyncMock
) -> None:
    await main_module.run_bot(settings())

    get_me.assert_awaited_once()
    start_polling.assert_awaited_once()
    bot = start_polling.await_args.args[0]
    assert isinstance(bot, Bot)
    assert bot.token == TOKEN
    assert start_polling.await_args.kwargs["allowed_updates"] == ["callback_query", "message"]
    # Flood control (retry after 429) is installed on the bot session.
    assert any(
        isinstance(middleware, FloodControlMiddleware) for middleware in bot.session.middleware
    )


def test_build_services_uses_the_fake_backend() -> None:
    services = main_module.build_services(settings())

    assert isinstance(services, Services)
    assert settings().app_services == "fake"


@pytest.mark.parametrize(
    ("overrides", "missing"),
    [({"telegram_bot_token": None}, "TELEGRAM_BOT_TOKEN"), ({"admin_ids": ""}, "ADMIN_IDS")],
)
async def test_run_bot_refuses_to_start_without_required_settings(
    start_polling: AsyncMock, get_me: AsyncMock, overrides: dict[str, object], missing: str
) -> None:
    with pytest.raises(ConfigurationError, match=missing):
        await main_module.run_bot(settings(**overrides))

    get_me.assert_not_awaited()
    start_polling.assert_not_awaited()


async def test_malformed_token_is_a_configuration_error(start_polling: AsyncMock) -> None:
    with pytest.raises(ConfigurationError, match="malformed") as caught:
        await main_module.run_bot(settings(telegram_bot_token="not-a-token"))

    assert "not-a-token" not in str(caught.value)
    start_polling.assert_not_awaited()


async def test_rejected_token_is_a_configuration_error_and_closes_session(
    monkeypatch: pytest.MonkeyPatch, start_polling: AsyncMock, session_close: AsyncMock
) -> None:
    error = TelegramUnauthorizedError(method=GetMe(), message="Unauthorized")
    monkeypatch.setattr(Bot, "get_me", AsyncMock(side_effect=error))

    with pytest.raises(ConfigurationError, match="unauthorized") as caught:
        await main_module.run_bot(settings())

    assert TOKEN not in str(caught.value)
    session_close.assert_awaited_once()
    start_polling.assert_not_awaited()


async def test_network_error_during_token_check_propagates_and_closes_session(
    monkeypatch: pytest.MonkeyPatch, start_polling: AsyncMock, session_close: AsyncMock
) -> None:
    error = TelegramNetworkError(method=GetMe(), message="timeout")
    monkeypatch.setattr(Bot, "get_me", AsyncMock(side_effect=error))

    with pytest.raises(TelegramNetworkError):
        await main_module.run_bot(settings())

    session_close.assert_awaited_once()
    start_polling.assert_not_awaited()


@pytest.mark.usefixtures("get_me")
def test_main_exits_cleanly_after_polling_stops(
    monkeypatch: pytest.MonkeyPatch, start_polling: AsyncMock
) -> None:
    monkeypatch.setattr(main_module, "get_settings", settings)

    assert main_module.main() == main_module.EXIT_OK
    start_polling.assert_awaited_once()


def test_main_reports_missing_secret_with_exit_code(
    monkeypatch: pytest.MonkeyPatch, start_polling: AsyncMock, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        main_module, "get_settings", lambda: settings(telegram_bot_token=None, app_env="prod")
    )

    assert main_module.main() == main_module.EXIT_CONFIGURATION_ERROR
    start_polling.assert_not_awaited()
    assert "TELEGRAM_BOT_TOKEN is not set" in capsys.readouterr().out


def test_main_reports_invalid_env_as_json_without_echoing_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    start_polling: AsyncMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HEALTHCHECKS_URL", "hc-ping-SECRETKEY")
    monkeypatch.setenv("ADMIN_IDS", "abc")
    get_settings.cache_clear()
    try:
        exit_code = main_module.main()
    finally:
        get_settings.cache_clear()

    output = capsys.readouterr().out
    assert exit_code == main_module.EXIT_CONFIGURATION_ERROR
    record = json_records(output)[-1]
    assert record["event"] == "startup_failed"
    assert record["level"] == "critical"
    assert "HEALTHCHECKS_URL" in str(record["reason"])
    assert "ADMIN_IDS" in str(record["reason"])
    assert "SECRETKEY" not in output
    assert "Traceback" not in output
    start_polling.assert_not_awaited()


@pytest.mark.usefixtures("get_me")
def test_main_logs_unexpected_errors_with_exit_code_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(main_module, "get_settings", lambda: settings(app_env="prod"))
    monkeypatch.setattr(
        Dispatcher, "start_polling", AsyncMock(side_effect=RuntimeError(f"boom {TOKEN}"))
    )

    assert main_module.main() == main_module.EXIT_UNEXPECTED_ERROR

    output = capsys.readouterr().out
    record = json_records(output)[-1]
    assert record["event"] == "fatal_error"
    assert "RuntimeError" in json.dumps(record["exception"])
    assert TOKEN not in output  # configured secrets are scrubbed from tracebacks too
