"""Bot middleware."""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import Chat, TelegramObject, User

from app.logging import get_logger

logger = get_logger(__name__)

# aiogram's middleware protocol is typed with `dict[str, Any]` / `Any`; we mirror it exactly.
type Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class WhitelistMiddleware(BaseMiddleware):
    """Drop every update that is not from a whitelisted user in a private chat with the bot.

    This is a personal bot: signals and stakes must never be posted where others can read
    them, so groups and channels are ignored even when an admin writes there. Updates without
    a chat (inline queries, callbacks on inline-mode messages) are ignored for the same reason.
    Rejected updates get no reply at all, so strangers cannot even confirm the bot is alive.
    Register it as an *outer* middleware on ``Dispatcher.update`` so it runs before any
    filter or handler (aiogram has already resolved ``event_from_user`` at that point).
    """

    def __init__(self, allowed_user_ids: frozenset[int]) -> None:
        """Store the whitelist; an empty set rejects everyone."""
        self._allowed_user_ids = allowed_user_ids

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        """Pass the update on only if it is a whitelisted user's private chat."""
        user: User | None = data.get("event_from_user")
        chat: Chat | None = data.get("event_chat")
        if not self._is_allowed(user, chat):
            logger.debug(
                "update_rejected",
                user_id=None if user is None else user.id,
                chat_type=None if chat is None else chat.type,
            )
            return None
        return await handler(event, data)

    def _is_allowed(self, user: User | None, chat: Chat | None) -> bool:
        return (
            user is not None
            and user.id in self._allowed_user_ids
            and chat is not None
            and chat.type == ChatType.PRIVATE
        )
