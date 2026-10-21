"""Suite-wide fixtures: isolation from process-global state shared by every test module."""

import logging
from collections.abc import Iterator

import pytest
import structlog
from aiogram import Bot

from app.config import Settings
from tests.bot.fakes import RecordingSession

# Every setting is read from an upper-case variable of the same name. Deriving the list from
# the model keeps it complete when new settings are added.
SETTINGS_ENV_VARS = tuple(name.upper() for name in Settings.model_fields)


@pytest.fixture(autouse=True)
def clean_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every test from the developer's or CI runner's shell environment.

    ``TZ`` in particular is commonly set by operating systems (e.g. ``:/etc/localtime``) and
    would otherwise make any ``Settings()`` built in a test fail or differ between machines.
    """
    for name in SETTINGS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """``configure_logging`` mutates process-global state; put it back after each test."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers, root.level = handlers, level
    structlog.reset_defaults()


@pytest.fixture
def session() -> RecordingSession:
    """Offline Telegram session that records requests instead of sending them."""
    return RecordingSession()


@pytest.fixture
def bot(session: RecordingSession) -> Bot:
    """A real aiogram ``Bot`` wired to the recording session (no network)."""
    return Bot(token="42:TEST", session=session)
