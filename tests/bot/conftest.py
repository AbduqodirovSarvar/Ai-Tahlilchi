"""Fixtures for bot tests."""

import pytest
from aiogram import Bot

from tests.bot.fakes import BotHarness, RecordingSession


@pytest.fixture
def harness(bot: Bot, session: RecordingSession) -> BotHarness:
    """The real dispatcher over fake services with a fixed clock."""
    return BotHarness(bot, session)
