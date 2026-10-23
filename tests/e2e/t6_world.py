"""T6 harness: the production dispatcher AND the production Telegram notifier on real services.

Everything the pipeline sends on its own (digests, updates, settlements) goes through the real
``TelegramNotifier`` into the same recording Telegram session the dispatcher answers on, so a
test sees exactly the messages a user would see and can press the buttons of a digest card.
The data provider is the scripted API-Football double of the pipeline tests (the HTTP layer is
covered separately with respx); the language model is a scripted ``JsonGenerator``.
"""

import json
import re
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import BaseStorage
from aiogram.methods import SendMessage
from pydantic import BaseModel

from app.bot.dispatcher import build_dispatcher
from app.bot.middleware import RateLimitMiddleware
from app.bot.notifier import TelegramNotifier
from app.bot.storage import SqliteStorage
from app.clients.gemini import GeminiError, GeminiResponse, GeminiUsage
from app.db.engine import create_engine, create_session_factory
from app.llm.budget import LlmBudget, LlmLimits
from app.llm.service import LlmService
from app.pipeline.context import PipelineContext
from app.pipeline.learning_settings import LearningMode, LearningSettings
from app.pipeline.ledger import Ledger
from app.pipeline.overlay import DbLearningSource, NoLearning
from app.pipeline.review import LlmCandidateReviewer
from app.scheduler import JOB_DAILY_TICK, JOB_RECALC_TICK, JOB_TEMPLATE_TICK, PipelineJobs
from app.services.interfaces import Services, UserSettingsView
from app.services.real import RealSystem, create_real_services
from tests.bot.fakes import RecordingSession
from tests.e2e.bot_client import Client
from tests.pipeline.harness import Env, open_env
from tests.pipeline.providers import ScriptedProvider, scripted_provider
from tests.pipeline.world import World

TASHKENT = ZoneInfo("Asia/Tashkent")
USER = 1001
OTHER = 1002
ADMINS = frozenset({USER, OTHER})
CANDIDATE_ID = re.compile(r"\b(\d+:[a-z_0-9]+:[a-z_0-9]+:[-0-9.]+)\b")


class ScriptedGemini:
    """A ``JsonGenerator`` that confirms every candidate it is shown, or fails on demand."""

    model = "gemini-t6"

    def __init__(self) -> None:
        self.requests = 0
        self.down = False
        self.verdict = "tasdiq"

    async def generate_json(
        self, *, system_instruction: str, contents: str, response_schema: type[BaseModel]
    ) -> GeminiResponse:
        """Answer from the candidate ids found in the prompt (the pipeline builds them)."""
        del system_instruction, response_schema
        self.requests += 1
        if self.down:
            msg = "Gemini request failed after 3 attempt(s): TimeoutError"
            raise GeminiError(msg)
        ids = list(dict.fromkeys(CANDIDATE_ID.findall(contents)))
        reviews = [
            {
                "candidate_id": cid,
                "verdict": self.verdict,
                "risk_flags": [],
                "izoh": "Salbiy omil ko'rinmaydi.",
            }
            for cid in ids
        ]
        text = json.dumps({"reviews": reviews}, ensure_ascii=False)
        return GeminiResponse(
            text=text,
            model=self.model,
            usage=GeminiUsage(input_tokens=400, output_tokens=60),
            attempts=1,
        )


@dataclass
class Week:
    """One running installation: database, services, notifier, dispatcher, Telegram session."""

    env: Env
    provider: ScriptedProvider
    gemini: ScriptedGemini
    tmp: Path
    session: RecordingSession
    bot: Bot
    ctx: PipelineContext
    system: RealSystem
    notifier: TelegramNotifier
    dispatcher: Dispatcher
    storage: BaseStorage
    extra_engines: list[Any] = field(default_factory=list)
    extra_storages: list[Any] = field(default_factory=list)
    clients: dict[int, Client] = field(default_factory=dict)

    @property
    def services(self) -> Services:
        """The services the dispatcher serves."""
        return self.system.services

    @property
    def clock(self):  # type: ignore[no-untyped-def]  # the MutableClock of the pipeline tests
        """The shared controllable clock."""
        return self.env.clock

    def client(self, user_id: int = USER) -> Client:
        """The user's chat (kept between calls, so button state survives)."""
        if user_id not in self.clients:
            self.clients[user_id] = Client(self.dispatcher, self.bot, self.session, user_id)
        return self.clients[user_id]

    def sync(self, user_id: int = USER) -> list[SendMessage]:
        """Teach the user's client about messages the pipeline pushed (digests, updates).

        Returns the pushed messages that were new to the client. The message id of a request is
        its 1-based position in the session, exactly what the recording session answered.
        """
        client = self.client(user_id)
        fresh: list[tuple[int, Any]] = []
        for index, method in enumerate(self.session.requests, start=1):
            if (
                isinstance(method, SendMessage)
                and method.chat_id == user_id
                and index not in client.texts
            ):
                fresh.append((index, method))
        client._track(fresh)
        return [method for _, method in fresh]

    def pushed(self, user_id: int = USER) -> list[SendMessage]:
        """Every message the bot sent to ``user_id`` so far (pushed and replies)."""
        return [
            m for m in self.session.requests if isinstance(m, SendMessage) and m.chat_id == user_id
        ]

    async def settings(self, user_id: int = USER, **changes: object) -> UserSettingsView:
        """Update fields of the user's settings."""
        current = await self.services.settings.get_settings(user_id)
        return await self.services.settings.update_settings(
            user_id,
            replace(current, **changes),  # type: ignore[arg-type]
        )

    async def restart(self) -> "Week":
        """A new process over the same database files: new engine, context, notifier, dispatcher.

        The Telegram session (the chat history), the clock and the scripted provider survive,
        like Telegram and the API do; everything in memory is rebuilt.
        """
        engine = create_engine(self.tmp / "week.db")
        sessions = create_session_factory(engine)
        ctx = replace(self.ctx, sessions=sessions, ledger=Ledger(sessions))
        notifier = TelegramNotifier(admin_ids=ADMINS, default_zone=TASHKENT)
        notifier.bind(self.bot)
        system = create_real_services(ctx, notifier=notifier)
        notifier.attach_settings(system.services.settings)
        storage = SqliteStorage(self.tmp / "fsm.db")
        dispatcher = build_dispatcher(
            ADMINS,
            system.services,
            storage=storage,
            clock=self.env.clock,
            rate_limit=RateLimitMiddleware(capacity=10_000),
        )
        reborn = replace(
            self,
            ctx=ctx,
            system=system,
            notifier=notifier,
            dispatcher=dispatcher,
            storage=storage,
            clients={},
        )
        self.extra_engines.append(engine)  # the lists are shared: the fixture closes them all
        self.extra_storages.append(storage)
        for user_id, old in self.clients.items():
            fresh = reborn.client(user_id)
            fresh.texts, fresh.markups = dict(old.texts), dict(old.markups)
        return reborn


def tg_signals(week: Week) -> list[str]:
    """Texts of every card message the bot sent (any user)."""
    return [m.text for m in week.session.requests if isinstance(m, SendMessage) and "🎯" in m.text]


@asynccontextmanager
async def open_week(  # noqa: PLR0913 - one switch per thing a test may vary
    history: "tuple[Path, World]",
    tmp: Path,
    bot: Bot,
    session: RecordingSession,
    *,
    llm_limits: LlmLimits | None = None,
    with_llm: bool = True,
    learning_mode: LearningMode = LearningMode.SHADOW,
) -> AsyncIterator[Week]:
    """The history-only database + scripted provider, a Telegram notifier and a dispatcher."""
    template, world = history
    gemini = ScriptedGemini()
    clock_box: list[Any] = []  # filled once the environment (and its clock) exists
    budget = LlmBudget(
        limits=llm_limits or LlmLimits(),
        clock=lambda: clock_box[0]() if clock_box else datetime.now(UTC),
    )
    llm = LlmService(gemini, budget=budget)
    reviewer = LlmCandidateReviewer(llm) if with_llm else None
    async with open_env(
        template,
        world,
        tmp / "week.db",
        provider_factory=lambda clock: scripted_provider(world, clock),
        reveal_results=False,
        reviewer=reviewer,
    ) as env:
        clock_box.append(env.clock)
        provider = env.ctx.providers[next(iter(env.ctx.providers))]
        assert isinstance(provider, ScriptedProvider)
        # mirror app.pipeline.runtime: only "apply" lets the learned rules reach the analysis
        ctx = replace(
            env.ctx,
            learning=DbLearningSource(env.sessions)
            if learning_mode is LearningMode.APPLY
            else NoLearning(),
            learning_settings=LearningSettings(mode=learning_mode),
        )
        notifier = TelegramNotifier(admin_ids=ADMINS, default_zone=TASHKENT)
        notifier.bind(bot)
        system = create_real_services(ctx, notifier=notifier)
        notifier.attach_settings(system.services.settings)
        storage = SqliteStorage(tmp / "fsm.db")
        dispatcher = build_dispatcher(
            ADMINS,
            system.services,
            storage=storage,
            clock=env.clock,
            rate_limit=RateLimitMiddleware(capacity=10_000),
        )
        week = Week(
            env=env,
            provider=provider,
            gemini=gemini,
            tmp=tmp,
            session=session,
            bot=bot,
            ctx=ctx,
            system=system,
            notifier=notifier,
            dispatcher=dispatcher,
            storage=storage,
        )
        try:
            yield week
        finally:
            await storage.close()
            for extra in week.extra_storages:
                await extra.close()
            for engine in week.extra_engines:
                await engine.dispose()


def jobs(week: Week) -> PipelineJobs:
    """The scheduler's own job wrapper over this installation (errors recorded, never raised)."""
    return PipelineJobs(week.ctx, week.system.delivery)


async def tick(week: Week, minutes: int, *, step: int = 30) -> None:
    """Move the clock in ``step``-minute strides, running the three minute ticks each time."""
    for _ in range(max(minutes // step, 1)):
        week.clock.advance(timedelta(minutes=step))
        runner = jobs(week)
        await runner.run(JOB_DAILY_TICK)
        await runner.run(JOB_TEMPLATE_TICK)
        await runner.run(JOB_RECALC_TICK)


def at(week: Week, day: int, hour: int, minute: int = 0) -> None:
    """Jump the clock to ``2026-10-<day> hour:minute`` UTC (never backwards)."""
    target = datetime(2026, 10, day, hour, minute, tzinfo=UTC)
    assert target >= week.clock(), f"{target} is before {week.clock()}"
    week.clock.now = target


PERMISSIVE: dict[str, Any] = {
    "min_probability": 0.30,
    "min_ev": 0.02,
    "quiet_hours": None,
    "bank": Decimal(1000),
    "unit": Decimal(10),
}


def sorted_ids(items: Sequence[Any]) -> list[int]:
    """Signal ids in ascending order."""
    return sorted(item.signal_id for item in items)


__all__ = ["ADMINS", "OTHER", "PERMISSIVE", "USER", "Week", "open_week", "timedelta"]
