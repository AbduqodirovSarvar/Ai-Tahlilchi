"""Fixtures for end-to-end bot tests: the real dispatcher over fake services."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher

from app.services.interfaces import Services
from tests.bot.fakes import ADMIN_ID, RecordingSession
from tests.e2e.bot_client import Client, make_dispatcher, services_of
from tests.e2e.real_world import RealBot, open_real_bot
from tests.pipeline.world import World

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


@pytest.fixture
async def real_bot(
    seeded_template: "tuple[Path, World]", tmp_path: Path, bot: Bot, session: RecordingSession
) -> AsyncIterator[RealBot]:
    """The production dispatcher over the real services (see ``real_world``)."""
    template, world = seeded_template
    async with open_real_bot(template, world, tmp_path / "real.db", bot, session) as opened:
        yield opened
