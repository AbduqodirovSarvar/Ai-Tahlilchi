"""The call log is for administrators only, in the fake and in the real services."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.services.errors import NotAuthorizedError
from app.services.fake import create_fake_services
from app.services.interfaces import CallLogRequest, Services
from app.services.real import create_real_services
from tests.pipeline.harness import open_env
from tests.pipeline.world import World
from tests.services.contract_suite import MutableClock

ADMIN = 7
STRANGER = 8


@pytest.fixture(params=["fake", "real"])
async def services(
    request: pytest.FixtureRequest, seeded_template: tuple[Path, World], tmp_path: Path
) -> AsyncIterator[Services]:
    admins = frozenset({ADMIN})
    if request.param == "fake":
        yield create_fake_services(clock=MutableClock(), admin_ids=admins)
        return
    template, world = seeded_template
    async with open_env(template, world, tmp_path / "real.db") as env:
        yield create_real_services(env.ctx, admin_ids=admins).services


async def test_a_non_admin_is_refused_everywhere(services: Services) -> None:
    with pytest.raises(NotAuthorizedError):
        await services.calls.list_calls(STRANGER, CallLogRequest())
    with pytest.raises(NotAuthorizedError):
        await services.calls.get_call(STRANGER, 1)
    with pytest.raises(NotAuthorizedError):
        await services.calls.call_stats(STRANGER)


async def test_the_refusal_comes_before_input_checks(services: Services) -> None:
    with pytest.raises(NotAuthorizedError):
        await services.calls.call_stats(STRANGER, days=999)


async def test_an_admin_may_read(services: Services) -> None:
    page = await services.calls.list_calls(ADMIN, CallLogRequest())
    assert page.page == 0
