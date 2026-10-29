"""``main.run_real`` hands the pipeline a Telegram notifier that binds to the bot at startup."""

from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot, Dispatcher

from app import main as main_module
from app.bot.notifier import TelegramNotifier
from app.config import Settings
from app.services.interfaces import Notifier, SettledNotice
from tests.bot.fakes import RecordingSession
from tests.bot.samples import key, make_signal


async def test_run_real_wires_the_telegram_notifier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    session = RecordingSession()
    bot = Bot(token="42:TEST", session=session)
    seen: dict[str, Any] = {}
    real_create = main_module.create_real_services

    def spy_create(
        ctx: Any, notifier: Notifier | None = None, *, build: str = "", matches: Any = None
    ) -> Any:
        seen["notifier"], seen["build"] = notifier, build
        return real_create(ctx, notifier=notifier, build=build, matches=matches)

    async def poll(
        dispatcher: Dispatcher, token: str, admins: frozenset[int], _s: Settings
    ) -> None:
        seen["dispatcher"], seen["token"], seen["admins"] = dispatcher, token, admins
        await dispatcher.emit_startup(bot=bot)  # what start_polling does before the first update

    monkeypatch.setattr(main_module, "create_real_services", spy_create)
    monkeypatch.setattr(main_module, "poll", poll)
    config = Settings(
        _env_file=None,  # type: ignore[call-arg]
        db_path=tmp_path / "wired.db",
        app_services="real",
        telegram_bot_token="42:TEST",
        admin_ids="1",
    )

    await main_module.run_bot(config)

    notifier = seen["notifier"]
    assert isinstance(notifier, TelegramNotifier)
    assert seen["dispatcher"]["backend"] == "real"
    assert seen["build"]  # the running build reaches /holat (label of app.ops.version)
    ref = await notifier.send_result_settled(1, SettledNotice(signal=make_signal()), key=key())
    assert ref is not None
    assert len(session.requests) == 1
