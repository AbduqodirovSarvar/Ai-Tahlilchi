"""Fixtures for end-to-end bot tests: the real dispatcher over fake services."""

import pytest
from aiogram import Bot, Dispatcher

from app.services.interfaces import Services
from tests.bot.fakes import ADMIN_ID, RecordingSession
from tests.e2e.bot_client import Client, make_dispatcher, services_of

SECOND_ADMIN_ID = 1002


@pytest.fixture
def dispatcher() -> Dispatcher:
    """Production dispatcher serving two admins."""
    return make_dispatcher(frozenset({ADMIN_ID, SECOND_ADMIN_ID}))


@pytest.fixture
def services(dispatcher: Dispatcher) -> Services:
    """The fake services behind ``dispatcher``."""
    return services_of(dispatcher)


@pytest.fixture
def admin(dispatcher: Dispatcher, bot: Bot, session: RecordingSession) -> Client:
    """The first admin in a private chat."""
    return Client(dispatcher, bot, session, ADMIN_ID)
