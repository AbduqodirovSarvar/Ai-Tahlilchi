"""QA: whitelist/private-only with the catch-all router, concurrency and state isolation."""

import asyncio
from typing import Any

import pytest
from aiogram import Bot, Dispatcher

from app.bot import texts
from app.bot.callbacks import CardCb, ConfirmCb, NavCb, ResultsCb, SettingsCb
from app.domain import Sport
from app.services.interfaces import AnalysisQuery, AnalysisResult, ScopeKind, Services, SortKey
from tests.bot.fakes import ADMIN_ID, STRANGER_ID, RecordingSession
from tests.e2e.bot_client import Client, YieldingSession, answers, make_dispatcher, shown
from tests.e2e.conftest import SECOND_ADMIN_ID
from tests.e2e.flows import to_confirm

GROUP_CHAT_ID = -100_555


class CountingAnalyze:
    """Counts ``analyze`` calls (and lets the event loop run in between, like real work)."""

    def __init__(self, services: Services) -> None:
        self.calls: list[AnalysisQuery] = []
        self._real = services.analysis.analyze
        services.analysis.analyze = self  # type: ignore[method-assign]

    async def __call__(self, user_id: int, query: AnalysisQuery, **kwargs: Any) -> AnalysisResult:
        self.calls.append(query)
        await asyncio.sleep(0)
        return await self._real(user_id, query, **kwargs)


# --------------------------------------------------------------------------- access
ANY_BUTTONS = [
    NavCb(action="back").pack(),
    ConfirmCb(action="start").pack(),
    CardCb(action="hide", signal_id=1).pack(),
    ResultsCb(page=1).pack(),
    SettingsCb(field="bank", action="open").pack(),
    "garbage-data",
]


@pytest.mark.parametrize("data", ANY_BUTTONS)
async def test_stranger_gets_nothing_even_via_buttons(
    dispatcher: Dispatcher, bot: Bot, session: RecordingSession, data: str
) -> None:
    stranger = Client(dispatcher, bot, session, STRANGER_ID)

    await stranger.send("/start")
    await stranger.send(texts.MENU_ANALYSIS)
    await stranger.press_data(data, 1)

    assert session.requests == []


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
@pytest.mark.parametrize("data", ANY_BUTTONS)
async def test_admin_outside_private_chat_gets_nothing(
    dispatcher: Dispatcher, bot: Bot, session: RecordingSession, chat_type: str, data: str
) -> None:
    in_group = Client(
        dispatcher, bot, session, ADMIN_ID, chat_id=GROUP_CHAT_ID, chat_type=chat_type
    )

    await in_group.send("/start")
    await in_group.send("/tahlil bugun")
    await in_group.send("salom")
    await in_group.press_data(data, 1)

    assert session.requests == []


async def test_stranger_cannot_hijack_admin_wizard_buttons(
    dispatcher: Dispatcher, bot: Bot, session: RecordingSession, admin: Client
) -> None:
    await to_confirm(admin)
    message_id, start = admin.find_button(texts.BTN_START)
    before = len(session.requests)
    # Same chat id as the admin's private chat, but another user presses (forged update).
    intruder = Client(dispatcher, bot, session, STRANGER_ID, chat_id=ADMIN_ID)

    await intruder.press_data(start, message_id)

    assert len(session.requests) == before
    assert await admin.state() == "AnalysisFlow:confirm"


# --------------------------------------------------------------------------- concurrency
async def test_concurrent_double_tap_on_start_runs_once_in_sequence() -> None:
    """Baseline: taps processed one after another (what tests/bot covers)."""
    session = YieldingSession()
    bot = Bot(token="42:TEST", session=session)
    dispatcher = make_dispatcher(frozenset({ADMIN_ID}))
    admin = Client(dispatcher, bot, session, ADMIN_ID)
    counter = CountingAnalyze(dispatcher["services"])
    await to_confirm(admin)
    message_id, start = admin.find_button(texts.BTN_START)

    await admin.press_data(start, message_id)
    second = await admin.press_data(start, message_id)

    assert len(counter.calls) == 1
    assert answers(second)[0].text in {texts.STALE_BUTTON, texts.ALREADY_RUNNING}


async def test_concurrent_double_tap_on_start_runs_analysis_once() -> None:
    session = YieldingSession()
    bot = Bot(token="42:TEST", session=session)
    dispatcher = make_dispatcher(frozenset({ADMIN_ID}))
    admin = Client(dispatcher, bot, session, ADMIN_ID)
    counter = CountingAnalyze(dispatcher["services"])
    await to_confirm(admin)
    message_id, start = admin.find_button(texts.BTN_START)

    await asyncio.gather(admin.press_data(start, message_id), admin.press_data(start, message_id))

    assert len(counter.calls) == 1


async def test_two_users_in_the_wizard_do_not_share_state() -> None:
    session = YieldingSession()
    bot = Bot(token="42:TEST", session=session)
    dispatcher = make_dispatcher(frozenset({ADMIN_ID, SECOND_ADMIN_ID}))
    first = Client(dispatcher, bot, session, ADMIN_ID)
    second = Client(dispatcher, bot, session, SECOND_ADMIN_ID)
    counter = CountingAnalyze(dispatcher["services"])

    async def first_flow() -> None:
        await to_confirm(first, date_button=texts.BTN_TODAY, probability="50%")

    async def second_flow() -> None:
        await second.send(texts.MENU_ANALYSIS)
        for label in (
            texts.BTN_TOMORROW,
            texts.SPORT_LABELS[Sport.FOOTBALL],
            "70%",
            "8%",
            texts.BTN_SCOPE_LEAGUES,
            "La Liga",
            texts.BTN_DONE,
            "3",
            texts.BTN_DONE,
            texts.SORT_LABELS[SortKey.KICKOFF],
        ):
            await second.press(label)

    await asyncio.gather(first_flow(), second_flow())

    assert "≥ 50% · EV ≥ 5%" in first.last_text
    assert "<b>Qamrov:</b> hamma o'yinlar" in first.last_text
    assert "≥ 70% · EV ≥ 8%" in second.last_text
    assert "<b>Qamrov:</b> La Liga" in second.last_text

    # A button of user 1 pressed by user 2 (forged, same message id) must not run user 1's query.
    message_id, start = first.find_button(texts.BTN_START)
    forged = await second.press_data(start, message_id)
    assert answers(forged)[0].text == texts.STALE_BUTTON
    assert counter.calls == []

    await asyncio.gather(first.press(texts.BTN_START), second.press(texts.BTN_START))
    by_probability = {round(query.min_probability, 2): query for query in counter.calls}
    assert set(by_probability) == {0.5, 0.7}
    assert by_probability[0.7].scope is ScopeKind.LEAGUES
    assert by_probability[0.5].scope is ScopeKind.ALL
    assert await first.state() is None
    assert await second.state() is None


async def test_high_probability_notice_is_per_user(
    dispatcher: Dispatcher, bot: Bot, session: RecordingSession, admin: Client
) -> None:
    other = Client(dispatcher, bot, session, SECOND_ADMIN_ID)
    for client in (admin, other):
        await client.send(texts.MENU_ANALYSIS)
        await client.press(texts.BTN_TODAY)
        await client.press(texts.SPORT_LABELS[Sport.FOOTBALL])
        methods = await client.press("90%")
        assert texts.HIGH_PROBABILITY_WARNING in shown(methods)[0]


async def test_tap_while_analysis_runs_is_told_to_wait() -> None:
    """A second ▶️ that arrives after the run started gets the 'already running' alert."""
    session = YieldingSession()
    bot = Bot(token="42:TEST", session=session)
    dispatcher = make_dispatcher(frozenset({ADMIN_ID}))
    services: Services = dispatcher["services"]
    admin = Client(dispatcher, bot, session, ADMIN_ID)
    release = asyncio.Event()
    started = asyncio.Event()
    real = services.analysis.analyze

    async def slow(user_id: int, query: AnalysisQuery, **kwargs: Any) -> AnalysisResult:
        started.set()
        await release.wait()
        return await real(user_id, query, **kwargs)

    services.analysis.analyze = slow  # type: ignore[method-assign]
    await to_confirm(admin)
    message_id, start = admin.find_button(texts.BTN_START)

    first = asyncio.create_task(admin.press_data(start, message_id))
    await started.wait()
    second = await admin.press_data(start, message_id)
    release.set()
    await first

    assert [answer.text for answer in answers(second)] == [texts.ALREADY_RUNNING]
    assert answers(second)[0].show_alert is True
