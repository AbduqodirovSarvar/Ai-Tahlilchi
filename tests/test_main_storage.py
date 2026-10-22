"""The bot runs with the persistent FSM storage next to the application database."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import User

from app import main as main_module
from app.bot.storage import FSM_DB_NAME, SqliteStorage
from app.config import Settings


async def test_polling_uses_sqlite_storage_and_closes_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dispatchers: list[Dispatcher] = []

    async def fake_start_polling(self: Dispatcher, *_args: object, **_kwargs: object) -> None:
        dispatchers.append(self)

    monkeypatch.setattr(Dispatcher, "start_polling", fake_start_polling)
    me = User(id=1, is_bot=True, first_name="B")
    monkeypatch.setattr(Bot, "get_me", AsyncMock(return_value=me))
    monkeypatch.setattr("aiogram.client.session.aiohttp.AiohttpSession.close", AsyncMock())
    closed = AsyncMock()
    monkeypatch.setattr(SqliteStorage, "close", closed)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        telegram_bot_token="42:TEST-TOKEN-VALUE",  # type: ignore[arg-type]
        admin_ids="1",  # type: ignore[arg-type]
        db_path=tmp_path / "app.db",
    )

    await main_module.run_bot(settings)

    assert isinstance(dispatchers[0].storage, SqliteStorage)
    assert not (tmp_path / FSM_DB_NAME).exists()  # opened lazily
    closed.assert_awaited_once()
