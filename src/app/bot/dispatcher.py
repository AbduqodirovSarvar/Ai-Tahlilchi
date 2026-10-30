"""Dispatcher assembly: the single place where middleware, routers and dependencies are wired.

Dependencies reach handlers through aiogram's workflow data (keyword arguments of
``Dispatcher``): ``services`` (the service contract), ``clock`` (UTC now, for "today" in
the user's time zone) and ``notices`` (one-time warnings).
"""

from collections.abc import Callable
from datetime import UTC, datetime

from aiogram import Dispatcher
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import ErrorEvent

from app.bot.handlers import analytics, fallback, start, status
from app.bot.jobs import BackgroundJobs
from app.bot.middleware import (
    ErrorMiddleware,
    RateLimitMiddleware,
    UserContextMiddleware,
    WhitelistMiddleware,
)
from app.bot.notices import OnceNotices
from app.logging import get_logger
from app.services.interfaces import Services

logger = get_logger(__name__)

type Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """Current UTC instant (default bot clock)."""
    return datetime.now(UTC)


async def log_unhandled_error(event: ErrorEvent) -> bool:
    """Last-resort handler: log any exception a handler leaked, keep polling alive."""
    logger.error(
        "unhandled_update_error",
        update_id=event.update.update_id,
        exc_info=event.exception,
    )
    return True


def build_dispatcher(
    admin_ids: frozenset[int],
    services: Services,
    *,
    storage: BaseStorage | None = None,
    clock: Clock = utc_now,
    rate_limit: RateLimitMiddleware | None = None,
) -> Dispatcher:
    """Create a dispatcher that only serves ``admin_ids`` and talks to ``services``.

    ``storage`` defaults to in-memory FSM storage (ADR 0006); pass a persistent one to
    keep wizard state across restarts. ``rate_limit`` defaults to the standard per-user
    token bucket.
    """
    # One update of a user/chat at a time: concurrent taps would otherwise start the
    # analysis twice and lose each other's read-modify-write of drafts and settings.
    isolation = SimpleEventIsolation()
    jobs = BackgroundJobs(isolation)
    dispatcher = Dispatcher(
        storage=storage or MemoryStorage(),
        events_isolation=isolation,
        services=services,
        clock=clock,
        notices=OnceNotices(),
        jobs=jobs,
    )
    dispatcher.shutdown.register(jobs.cancel_all)
    # aiogram registers its FSM middleware (which takes the per-user isolation lock and never
    # releases the lock object) before ours. Move it behind the whitelist so a stranger is
    # dropped before it can leave anything in the process (T6-F-8).
    dispatcher.update.outer_middleware.unregister(dispatcher.fsm)
    dispatcher.update.outer_middleware(WhitelistMiddleware(admin_ids))
    dispatcher.update.outer_middleware(dispatcher.fsm)
    dispatcher.update.outer_middleware(UserContextMiddleware())
    dispatcher.update.outer_middleware(rate_limit or RateLimitMiddleware())
    dispatcher.message.middleware(ErrorMiddleware())
    dispatcher.callback_query.middleware(ErrorMiddleware())
    dispatcher.errors.register(log_unhandled_error)
    # Order matters: commands and menu first, catch-all last.
    for module in (start, analytics, status, fallback):
        dispatcher.include_router(module.create_router())
    return dispatcher
