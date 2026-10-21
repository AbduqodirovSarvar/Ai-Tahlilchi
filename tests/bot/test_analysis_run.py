"""Tests of the progress reporter (throttling, never raising)."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import Chat, Message

from app.bot.handlers.analysis_run import PROGRESS_MIN_INTERVAL, ProgressReporter
from app.services.interfaces import AnalysisProgress, AnalysisStage
from tests.bot.fakes import RecordingSession


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def progress_message(bot: Bot) -> Message:
    return Message(
        message_id=9, date=datetime.now(UTC), chat=Chat(id=1, type="private"), text="⏳"
    ).as_(bot)


def step(stage: AnalysisStage, done: int, total: int = 10) -> AnalysisProgress:
    return AnalysisProgress(stage=stage, done=done, total=total)


async def test_edits_are_throttled_but_final_stage_always_shows(
    bot: Bot, session: RecordingSession
) -> None:
    clock = Clock()
    reporter = ProgressReporter(progress_message(bot), clock)

    clock.now += PROGRESS_MIN_INTERVAL
    await reporter(step(AnalysisStage.ANALYSING, 1))
    await reporter(step(AnalysisStage.ANALYSING, 2))  # too soon: skipped
    clock.now += PROGRESS_MIN_INTERVAL
    await reporter(step(AnalysisStage.ANALYSING, 3))
    await reporter(step(AnalysisStage.FINISHED, 10))
    await reporter(step(AnalysisStage.FINISHED, 10))  # same text: skipped

    edits = [m.text for m in session.requests if isinstance(m, EditMessageText)]
    assert edits == [
        "⏳ Tahlil qilinmoqda… 1/10",
        "⏳ Tahlil qilinmoqda… 3/10",
        "✅ Tahlil tugadi: 10 ta o'yin ko'rib chiqildi.",
    ]


async def test_failed_edit_never_raises(bot: Bot, monkeypatch: pytest.MonkeyPatch) -> None:
    error = TelegramBadRequest(method=SendMessage(chat_id=1, text="x"), message="bad")
    monkeypatch.setattr(Message, "edit_text", AsyncMock(side_effect=error))
    reporter = ProgressReporter(progress_message(bot), Clock())

    await reporter(step(AnalysisStage.FINISHED, 10))
