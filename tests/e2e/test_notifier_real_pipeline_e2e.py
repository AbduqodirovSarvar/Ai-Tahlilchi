"""The Telegram notifier under the real pipeline: real DTOs, real digest, real ledger.

The unit tests build DTOs by hand; this one lets the scheduler's jobs produce them over the
seeded database, so a mismatch between what the pipeline sends and what the bot can render
(a field the formatter does not expect, an id that does not fit a button) shows up here.
"""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot
from aiogram.methods import SendMessage

from app.bot import texts
from app.bot.notifier import TelegramNotifier
from app.pipeline.daily import run_due_digests
from app.pipeline.prematch import run_due_recalcs
from app.services.interfaces import AnalysisQuery, DateSpec
from app.services.real import RealSystem, create_real_services
from tests.bot.fakes import RecordingSession
from tests.e2e.bot_client import CALLBACK_DATA_LIMIT, telegram_html_problems
from tests.pipeline.harness import Env, open_env
from tests.pipeline.world import World

USER = 2002
TASHKENT = ZoneInfo("Asia/Tashkent")


@pytest.fixture
async def wired(
    seeded_template: "tuple[Path, World]", tmp_path: Path
) -> AsyncIterator[tuple[Env, RealSystem, RecordingSession]]:
    """The seeded world with the real services delivering through a Telegram notifier."""
    template, world = seeded_template
    async with open_env(template, world, tmp_path / "wired.db") as env:
        session = RecordingSession()
        notifier = TelegramNotifier(admin_ids=frozenset({USER}), default_zone=TASHKENT)
        notifier.bind(Bot(token="42:TEST", session=session))
        system = create_real_services(env.ctx, notifier=notifier)
        notifier.attach_settings(system.services.settings)
        yield env, system, session


async def permissive(system: RealSystem) -> None:
    settings = await system.services.settings.get_settings(USER)
    await system.services.settings.update_settings(
        USER, replace(settings, min_probability=0.30, min_ev=0.0, quiet_hours=None)
    )


def messages(session: RecordingSession) -> list[SendMessage]:
    return [m for m in session.requests if isinstance(m, SendMessage)]


async def test_the_real_digest_is_delivered_as_cards_with_valid_buttons(
    wired: tuple[Env, RealSystem, RecordingSession],
) -> None:
    env, system, session = wired
    await permissive(system)
    env.clock.advance(timedelta(hours=5))  # past the 09:00 digest time of the user

    report = await run_due_digests(env.ctx, system.delivery)

    assert report.sent == (USER,)
    sent = messages(session)
    assert sent[0].text.startswith("☀️ <b>Kunlik tahlil</b>")
    for message in sent:
        assert telegram_html_problems(message.text) == [], message.text
        markup = message.reply_markup
        for row in getattr(markup, "inline_keyboard", []):
            for button in row:
                assert len((button.callback_data or "").encode()) <= CALLBACK_DATA_LIMIT
    card_texts = [m.text for m in sent if "🎯 Tanlov" in m.text]
    assert card_texts, "the seeded world must produce at least one signal"
    assert len(card_texts) == len(sent) - 1 - sum(1 for m in sent if m.text.startswith("📅"))
    again = await run_due_digests(env.ctx, system.delivery)
    assert again.sent == ()  # the ledger key is spent: no second digest
    assert len(messages(session)) == len(sent)


async def test_scheduled_updates_arrive_as_replies_to_the_digest(
    wired: tuple[Env, RealSystem, RecordingSession],
) -> None:
    env, system, session = wired
    await permissive(system)
    await system.services.analysis.analyze(
        USER, AnalysisQuery(dates=DateSpec.next_days(2), min_probability=0.3, min_ev=0.0)
    )
    env.clock.advance(timedelta(hours=5))
    await run_due_digests(env.ctx, system.delivery)
    before = len(messages(session))

    notified = 0
    for _ in range(48):
        env.clock.advance(timedelta(minutes=30))
        notified += (await run_due_recalcs(env.ctx, system.delivery)).notified

    updates = messages(session)[before:]
    assert notified == len(updates) > 0
    for message in updates:
        assert telegram_html_problems(message.text) == [], message.text
        assert message.parse_mode == "HTML"
        assert "Koeff.:" in message.text or texts.CANCELLED_TITLE.split("{")[0] in message.text
