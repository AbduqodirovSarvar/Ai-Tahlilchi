"""The real dispatcher over the REAL pipeline-backed services (QA harness for the T5 bot).

Same pattern as ``tests/services/test_real_contract.py`` (seeded SQLite copy, controllable
clock) but the services sit behind the production dispatcher, so a test can press a button and
check what the database really holds. Settlement hooks are off: results come only from manual
marks, so a test knows every bet's outcome in advance.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import BaseStorage

from app.bot.dispatcher import build_dispatcher
from app.bot.middleware import RateLimitMiddleware
from app.services.interfaces import (
    BetDraft,
    BetView,
    DateSpec,
    ResultMark,
    ResultMarkRequest,
    Services,
    SignalView,
    UserSettingsView,
)
from tests.bot.fakes import RecordingSession
from tests.e2e.bot_client import Client
from tests.pipeline.harness import Env, open_env
from tests.pipeline.world import World
from tests.services.contract_suite import analyze_week

ADMIN = 1001
SECOND = 1002
STRANGER = 777_000
SETTLE_AFTER = timedelta(days=4)
"""Past the last kickoff of a ``next_days(3)`` analysis."""


@dataclass
class RealBot:
    """Dispatcher, services and a client factory sharing one seeded database."""

    env: Env
    dispatcher: Dispatcher
    bot: Bot
    session: RecordingSession

    @property
    def services(self) -> Services:
        """The real services behind the dispatcher."""
        return self.env.system.services

    def client(self, user_id: int = ADMIN) -> Client:
        """A user in a private chat with the bot."""
        return Client(self.dispatcher, self.bot, self.session, user_id)

    async def settings(self, user_id: int = ADMIN, **changes: object) -> UserSettingsView:
        """Update the user's settings fields."""
        current = await self.services.settings.get_settings(user_id)
        return await self.services.settings.update_settings(
            user_id,
            replace(current, **changes),  # type: ignore[arg-type]
        )

    async def signals(self, user_id: int = ADMIN, *, distinct: int = 1) -> list[SignalView]:
        """Recommended signals of the next three days, at most one per fixture."""
        result = await analyze_week(self.services, user_id, dates=DateSpec.next_days(3))
        chosen: dict[str, SignalView] = {}
        for signal in result.signals:
            if signal.recommended:
                chosen.setdefault(signal.fixture_id, signal)
        assert len(chosen) >= distinct, "the seeded world must have enough fixtures"
        return list(chosen.values())

    async def bet(
        self, signal: SignalView, stake: int, *, odds: float = 2.0, user_id: int = ADMIN
    ) -> BetView:
        """Record a bet with an explicit stake."""
        from decimal import Decimal  # noqa: PLC0415

        draft = BetDraft(signal_id=signal.signal_id, odds_taken=odds, stake=Decimal(stake))
        return await self.services.bets.place_bet(user_id, draft)

    def finish_matches(self) -> None:
        """Move the clock past every kickoff (no automatic settlement happens)."""
        self.env.clock.advance(SETTLE_AFTER)

    async def mark(
        self, signal: SignalView, mark: ResultMark, *, user_id: int = ADMIN, units: object = None
    ) -> None:
        """Settle ``signal`` by hand (as the bank screen does)."""
        request = ResultMarkRequest(
            signal_id=signal.signal_id,
            mark=mark,
            cashout_profit_units=units,  # type: ignore[arg-type]
        )
        await self.services.results.mark_result(user_id, request)


@asynccontextmanager
async def open_real_bot(
    template: Path,
    world: World,
    target: Path,
    bot: Bot,
    session: RecordingSession,
    *,
    storage: BaseStorage | None = None,
    rate_limit: RateLimitMiddleware | None = None,
    admin_ids: frozenset[int] = frozenset({ADMIN, SECOND}),
) -> AsyncIterator[RealBot]:
    """A real-services environment with a production dispatcher on top."""
    async with open_env(template, world, target, reveal_results=False) as env:
        dispatcher = build_dispatcher(
            admin_ids,
            env.system.services,
            storage=storage,
            clock=env.clock,
            rate_limit=rate_limit or RateLimitMiddleware(capacity=10_000),
        )
        yield RealBot(env, dispatcher, bot, session)
