"""Offline Telegram test doubles: a session that records requests instead of sending them."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any, override

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import Chat, Message, Update, User

ADMIN_ID = 1001
STRANGER_ID = 2002


class RecordingSession(BaseSession):
    """Session that stores every API call and answers with a plausible result."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []

    @override
    async def make_request(
        self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None
    ) -> Any:
        self.requests.append(method)
        return Message(
            message_id=len(self.requests),
            date=datetime.now(UTC),
            chat=Chat(id=getattr(method, "chat_id", 0), type="private"),
            text=getattr(method, "text", None),
        )

    @override
    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes]:
        raise NotImplementedError
        yield b""

    @override
    async def close(self) -> None:
        return None


def text_update(user_id: int, text: str, update_id: int = 1) -> Update:
    """Build a private-chat text message update as Telegram would deliver it."""
    user = User(id=user_id, is_bot=False, first_name="Test")
    message = Message(
        message_id=update_id,
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type="private"),
        from_user=user,
        text=text,
    )
    return Update(update_id=update_id, message=message)
