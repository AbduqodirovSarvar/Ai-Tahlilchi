"""QA: whitelist/private-only with the catch-all router, concurrency and state isolation."""

import asyncio
from typing import Any

import pytest
from aiogram import Bot, Dispatcher

from app.bot import texts
from app.bot.callbacks import CardCb, ConfirmCb, NavCb, ResultsCb, SettingsCb
from app.services.interfaces import AnalysisQuery, AnalysisResult, Services
from tests.bot.fakes import ADMIN_ID, STRANGER_ID, RecordingSession
from tests.e2e.bot_client import Client

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


# --------------------------------------------------------------------------- concurrency
