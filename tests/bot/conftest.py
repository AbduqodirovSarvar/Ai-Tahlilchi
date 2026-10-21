"""Fixtures for bot tests: a real aiogram Bot wired to an offline recording session."""

import pytest
from aiogram import Bot

from tests.bot.fakes import RecordingSession


@pytest.fixture
def session() -> RecordingSession:
    return RecordingSession()


@pytest.fixture
def bot(session: RecordingSession) -> Bot:
    return Bot(token="42:TEST", session=session)
