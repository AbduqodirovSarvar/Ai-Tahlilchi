"""Dispatcher assembly: the single place where middleware and routers are wired."""

from aiogram import Dispatcher
from aiogram.types import ErrorEvent

from app.bot.handlers import start
from app.bot.middleware import WhitelistMiddleware
from app.logging import get_logger

logger = get_logger(__name__)


async def log_unhandled_error(event: ErrorEvent) -> bool:
    """Last-resort handler: log any exception a handler leaked, keep polling alive."""
    logger.error(
        "unhandled_update_error",
        update_id=event.update.update_id,
        exc_info=event.exception,
    )
    return True


def build_dispatcher(admin_ids: frozenset[int]) -> Dispatcher:
    """Create a dispatcher that only serves ``admin_ids``."""
    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(WhitelistMiddleware(admin_ids))
    dispatcher.errors.register(log_unhandled_error)
    dispatcher.include_router(start.create_router())
    return dispatcher
