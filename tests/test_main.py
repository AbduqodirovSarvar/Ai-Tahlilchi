"""Tests for app.main (no network: polling is replaced by a mock)."""

import logging
from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest
import structlog
from aiogram import Bot, Dispatcher

from app import main as main_module
from app.config import Settings
from app.errors import ConfigurationError


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """main() configures logging globally; undo it after each test."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers, root.level = handlers, level
    structlog.reset_defaults()


@pytest.fixture
def start_polling(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock()
    monkeypatch.setattr(Dispatcher, "start_polling", mock)
    return mock


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"telegram_bot_token": "42:TEST", "admin_ids": "1"}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


async def test_run_bot_starts_polling_with_configured_token(start_polling: AsyncMock) -> None:
    await main_module.run_bot(settings())

    start_polling.assert_awaited_once()
    bot = start_polling.await_args.args[0]
    assert isinstance(bot, Bot)
    assert bot.token == "42:TEST"
    assert start_polling.await_args.kwargs["allowed_updates"] == ["message"]


@pytest.mark.parametrize(
    ("overrides", "missing"),
    [({"telegram_bot_token": None}, "TELEGRAM_BOT_TOKEN"), ({"admin_ids": ""}, "ADMIN_IDS")],
)
async def test_run_bot_refuses_to_start_without_required_settings(
    start_polling: AsyncMock, overrides: dict[str, object], missing: str
) -> None:
    with pytest.raises(ConfigurationError, match=missing):
        await main_module.run_bot(settings(**overrides))

    start_polling.assert_not_awaited()


def test_main_exits_cleanly_after_polling_stops(
    monkeypatch: pytest.MonkeyPatch, start_polling: AsyncMock
) -> None:
    monkeypatch.setattr(main_module, "get_settings", settings)

    assert main_module.main() == 0
    start_polling.assert_awaited_once()


def test_main_reports_configuration_error_with_exit_code(
    monkeypatch: pytest.MonkeyPatch, start_polling: AsyncMock, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        main_module, "get_settings", lambda: settings(telegram_bot_token=None, app_env="prod")
    )

    assert main_module.main() == main_module.EXIT_CONFIGURATION_ERROR
    start_polling.assert_not_awaited()
    assert "TELEGRAM_BOT_TOKEN is not set" in capsys.readouterr().out
