"""Per-update context of the F7 feature flows (custom analysis, watchlist, templates, ...).

One small object instead of five handler parameters: the services, the user, the FSM state, the
background jobs and the clock. :class:`FlowContextMiddleware` builds it for every update of a
router that registers it.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware
from aiogram.fsm.context import FSMContext
from aiogram.types import TelegramObject

from app.bot.jobs import BackgroundJobs
from app.services.interfaces import Services, UserSettingsView

type Clock = Callable[[], datetime]
type Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]

FLOW_KEY = "flow"


@dataclass(frozen=True, slots=True)
class FlowContext:
    """What a feature handler needs about the update it serves."""

    services: Services
    user_id: int
    state: FSMContext
    jobs: BackgroundJobs
    clock: Clock

    async def settings(self) -> UserSettingsView:
        """The user's current settings."""
        return await self.services.settings.get_settings(self.user_id)

    async def zone(self) -> ZoneInfo:
        """The user's time zone (all displayed times and dates use it)."""
        return (await self.settings()).zone

    async def today(self) -> date:
        """The user's local date."""
        return self.clock().astimezone(await self.zone()).date()


class FlowContextMiddleware(BaseMiddleware):
    """Inject ``flow`` (a :class:`FlowContext`) into the handlers of a feature router."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        """Build the context from the dispatcher's workflow data and run the handler."""
        data[FLOW_KEY] = FlowContext(
            services=data["services"],
            user_id=data["user_id"],
            state=data["state"],
            jobs=data["jobs"],
            clock=data["clock"],
        )
        return await handler(event, data)
