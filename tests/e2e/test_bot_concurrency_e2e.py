"""Concurrent updates of one user (QA F-1 and the review's lost-update findings).

aiogram polls with ``handle_as_tasks=True``: two taps arrive as two tasks. The dispatcher
serialises them per user (event isolation), and the long analysis runs in a background
job, so the lock is never held for the whole run.
"""

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import SendMessage

from app.bot import texts
from app.bot.formatters import cooldown_message
from app.bot.handlers.analysis_run import RESULTS_PAGE_SIZE
from app.domain import MarketType
from app.services.errors import CooldownActiveError
from app.services.interfaces import AnalysisQuery, AnalysisResult, Services
from tests.bot.fakes import ADMIN_ID
from tests.e2e.bot_client import Client, YieldingSession, answers, make_dispatcher, sent, shown
from tests.e2e.flows import reach, to_confirm


@pytest.fixture
def slow_admin() -> tuple[Client, Services]:
    """An admin whose every Telegram call yields to the event loop (like real I/O)."""
    session = YieldingSession()
    bot = Bot(token="42:TEST", session=session)
    dispatcher: Dispatcher = make_dispatcher(frozenset({ADMIN_ID}))
    services: Services = dispatcher["services"]
    return Client(dispatcher, bot, session, ADMIN_ID), services


def add_storage_latency(services: Services) -> None:
    """Make the settings service yield after each call, as a database would."""
    real_get, real_update = services.settings.get_settings, services.settings.update_settings

    async def get_settings(user_id: int) -> Any:
        settings = await real_get(user_id)
        await asyncio.sleep(0)
        return settings

    async def update_settings(user_id: int, settings: Any) -> Any:
        await asyncio.sleep(0)
        return await real_update(user_id, settings)

    services.settings.get_settings = get_settings  # type: ignore[method-assign]
    services.settings.update_settings = update_settings  # type: ignore[method-assign]


class BlockedAnalyze:
    """``analyze`` that waits until released, then returns the real result or raises."""

    def __init__(self, services: Services, failure: Exception | None = None) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self._failure = failure
        self._real = services.analysis.analyze
        services.analysis.analyze = self  # type: ignore[method-assign]

    async def __call__(self, user_id: int, query: AnalysisQuery, **kwargs: Any) -> AnalysisResult:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        if self._failure is not None:
            raise self._failure
        return await self._real(user_id, query, **kwargs)


async def stored_markets(client: Client, key: str) -> set[str]:
    """Markets in the wizard draft (``key="draft"``) of ``client``."""
    data = await client.dispatcher.storage.get_data(client.storage_key())
    return set(data[key]["markets"])


# --------------------------------------------------------------------------- lost updates
async def test_simultaneous_settings_toggles_are_both_saved(
    slow_admin: tuple[Client, Services],
) -> None:
    admin, services = slow_admin
    add_storage_latency(services)
    await admin.send(texts.MENU_SETTINGS)
    await admin.press(texts.SETTINGS_BUTTONS["markets"])
    first = admin.find_button(texts.MARKET_LABELS[MarketType.BTTS])
    second = admin.find_button(texts.MARKET_LABELS[MarketType.ASIAN_HANDICAP])

    await asyncio.gather(
        admin.press_data(first[1], first[0]), admin.press_data(second[1], second[0])
    )

    saved = (await services.settings.get_settings(ADMIN_ID)).markets
    assert {MarketType.BTTS, MarketType.ASIAN_HANDICAP} <= saved


async def test_simultaneous_wizard_market_toggles_are_both_kept(
    slow_admin: tuple[Client, Services],
) -> None:
    admin, _ = slow_admin
    await reach(admin, "markets")
    first = admin.find_button(texts.MARKET_LABELS[MarketType.BTTS])
    second = admin.find_button(texts.MARKET_LABELS[MarketType.DOUBLE_CHANCE])

    await asyncio.gather(
        admin.press_data(first[1], first[0]), admin.press_data(second[1], second[0])
    )

    assert {MarketType.BTTS.value, MarketType.DOUBLE_CHANCE.value} <= await stored_markets(
        admin, "draft"
    )


# --------------------------------------------------------------------------- pagination
async def test_double_tap_on_more_sends_the_page_once(
    slow_admin: tuple[Client, Services],
) -> None:
    admin, _ = slow_admin
    await admin.send("/tahlil 7kun futbol 30% ev0% 100ta")
    await admin.press(texts.BTN_START)
    message_id, more = admin.find_button("Yana")

    first, second = await asyncio.gather(
        admin.press_data(more, message_id), admin.press_data(more, message_id)
    )

    # Both calls see the chat's requests of the whole interval: count each message once.
    unique = list({id(m): m for m in sent(first + second)}.values())
    cards = [m for m in unique if m.text.startswith("⚽")]
    assert len(cards) == RESULTS_PAGE_SIZE
    assert len(answers(first)) == len(answers(second)) == 1


async def test_next_page_is_still_served_after_a_repeated_tap(
    slow_admin: tuple[Client, Services],
) -> None:
    admin, _ = slow_admin
    await admin.send("/tahlil 7kun futbol 30% ev0% 100ta")
    await admin.press(texts.BTN_START)
    message_id, more = admin.find_button("Yana")
    await admin.press_data(more, message_id)
    repeated = await admin.press_data(more, message_id)

    assert [m for m in sent(repeated) if m.text.startswith("⚽")] == []
    third_page = await admin.press("Yana")

    assert [m for m in sent(third_page) if m.text.startswith("⚽")]


async def test_more_button_of_an_older_result_is_refused(
    slow_admin: tuple[Client, Services],
) -> None:
    admin, _ = slow_admin
    await admin.send("/tahlil 7kun futbol 30% ev0% 100ta")
    await admin.press(texts.BTN_START)
    old_message, old_more = admin.find_button("Yana")
    await admin.send("/tahlil 7kun futbol 30% ev0% 100ta")
    await admin.press(texts.BTN_START)

    methods = await admin.press_data(old_more, old_message)

    assert [a.text for a in answers(methods)] == [texts.STALE_BUTTON]
    assert sent(methods) == []
    # The newest result's own button still works.
    assert [m for m in sent(await admin.press("Yana")) if m.text.startswith("⚽")]


# --------------------------------------------------------------------------- the long run
async def test_other_buttons_work_while_the_analysis_runs(
    slow_admin: tuple[Client, Services],
) -> None:
    admin, services = slow_admin
    blocked = BlockedAnalyze(services)
    await to_confirm(admin)

    run = asyncio.create_task(admin.press(texts.BTN_START))
    await blocked.started.wait()
    try:
        async with asyncio.timeout(2):
            methods = await admin.send(texts.MENU_SETTINGS)
    finally:
        blocked.release.set()
        await run

    assert shown(methods)[0].startswith("⚙️")
    assert blocked.calls == 1


async def test_progress_message_is_edited_by_the_background_run(
    slow_admin: tuple[Client, Services],
) -> None:
    admin, _ = slow_admin
    await to_confirm(admin)

    methods = await admin.press(texts.BTN_START)

    assert texts.PROGRESS.format(done=0, total="…") in shown(methods)
    assert any(text.startswith("✅ Tahlil tugadi") for text in admin.texts.values())
    assert await admin.state() is None


async def test_service_error_does_not_overwrite_a_newer_wizard(
    slow_admin: tuple[Client, Services],
) -> None:
    admin, services = slow_admin
    error = CooldownActiveError(timedelta(minutes=3))
    blocked = BlockedAnalyze(services, failure=error)
    await to_confirm(admin)
    old_wizard = admin.find_button(texts.BTN_START)[0]

    run = asyncio.create_task(admin.press(texts.BTN_START))
    await blocked.started.wait()
    await admin.send(texts.MENU_ANALYSIS)
    newer_wizard = admin.last_message_id
    blocked.release.set()
    methods = await run

    # The user's new wizard is untouched ...
    assert await admin.state() == "AnalysisFlow:dates"
    assert admin.markups[newer_wizard] is not None
    # ... the old wizard message is not turned back into a confirmation screen ...
    assert texts.WIZARD_STARTED in admin.texts[old_wizard]
    # ... but the user still learns why the run stopped.
    assert cooldown_message(error) in shown(methods)


async def test_finished_run_does_not_close_a_newer_wizard(
    slow_admin: tuple[Client, Services],
) -> None:
    admin, services = slow_admin
    blocked = BlockedAnalyze(services)
    await to_confirm(admin)

    run = asyncio.create_task(admin.press(texts.BTN_START))
    await blocked.started.wait()
    await admin.send(texts.MENU_ANALYSIS)
    blocked.release.set()
    methods = await run

    assert await admin.state() == "AnalysisFlow:dates"
    assert any(isinstance(m, SendMessage) and m.text.startswith("📊") for m in methods)


async def test_cards_of_an_old_result_work_during_a_new_run(
    slow_admin: tuple[Client, Services],
) -> None:
    admin, services = slow_admin
    await admin.send("/tahlil 3kun 50% ev3%")
    await admin.press(texts.BTN_START)
    card_message, hide = admin.find_button(texts.BTN_HIDE)
    blocked = BlockedAnalyze(services)
    await to_confirm(admin)

    run = asyncio.create_task(admin.press(texts.BTN_START))
    await blocked.started.wait()
    try:
        methods = await admin.press_data(hide, card_message)
    finally:
        blocked.release.set()
        await run

    assert [a.text for a in answers(methods)] == [texts.HIDDEN_ALERT]


async def test_wizard_is_dropped_when_the_confirmation_cannot_be_restored(
    slow_admin: tuple[Client, Services],
) -> None:
    admin, services = slow_admin
    error = CooldownActiveError(timedelta(minutes=1))
    BlockedAnalyze(services, failure=error).release.set()
    await to_confirm(admin)
    # From now on the cost estimate fails too (service outage), so the confirmation
    # screen cannot be shown again.
    estimates = AsyncMock(side_effect=RuntimeError("db down"))
    services.analysis.estimate_cost = estimates  # type: ignore[method-assign]

    methods = await admin.press(texts.BTN_START)

    assert await admin.state() is None
    assert cooldown_message(error) in shown(methods)
