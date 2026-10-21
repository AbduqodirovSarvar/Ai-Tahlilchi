"""Bot middleware."""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User

from app.logging import get_logger

logger = get_logger(__name__)

# aiogram's middleware protocol is typed with `dict[str, Any]` / `Any`; we mirror it exactly.
type Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class WhitelistMiddleware(BaseMiddleware):
    """Drop every update that does not come from a whitelisted Telegram user.

    Rejected updates get no reply at all, so strangers cannot even confirm the bot is alive.
    Register it as an *outer* middleware on ``Dispatcher.update`` so it runs before any
    filter or handler (aiogram has already resolved ``event_from_user`` at that point).
    """

    def __init__(self, allowed_user_ids: frozenset[int]) -> None:
        """Store the whitelist; an empty set rejects everyone."""
        self._allowed_user_ids = allowed_user_ids

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        """Pass the update on only if its sender is whitelisted."""
        user: User | None = data.get("event_from_user")
        if user is None or user.id not in self._allowed_user_ids:
            logger.info("update_rejected", user_id=None if user is None else user.id)
            return None
        return await handler(event, data)
