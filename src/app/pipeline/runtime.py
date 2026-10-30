"""Production wiring of the pipeline: engine, ledger, LLM reviewer and the live match feed.

``open_runtime`` is an async context manager that owns the database engine and the
Football-Data.org HTTP client and closes them on exit. One client (one per-minute pacer) serves
both the live match list and the pipeline's provider, so together they never exceed the plan's
limit. Without ``FOOTBALL_DATA_ORG_KEY`` the runtime is database-only (no provider): anything
that needs the provider is skipped or refused with a clear error (ADR 0025).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta

import httpx
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.football_data_org import create_football_data_client, usage_counter
from app.config import Settings
from app.db.api_usage import SqlUsageStore
from app.db.call_log import SqlCallRecorder
from app.db.engine import (
    create_engine_from_settings,
    create_session_factory,
    create_usage_engine,
)
from app.domain import BOOKMAKER_MARKET_AVERAGE, CallRecorder, Sport
from app.llm.service import LlmService, create_llm_service
from app.logging import get_logger
from app.pipeline.clock import Clock, system_clock
from app.pipeline.config import PipelineConfig, StakingMode
from app.pipeline.context import PipelineContext
from app.pipeline.experience import DbExperience
from app.pipeline.learning import promote_shadow_state
from app.pipeline.learning_settings import LearningMode, LearningSettings
from app.pipeline.ledger import Ledger
from app.pipeline.matches import MatchFeed
from app.pipeline.overlay import DbLearningSource, LearningSource, NoLearning
from app.pipeline.provider import FootballDataProvider, SportProvider
from app.pipeline.redaction import scrub_text
from app.pipeline.review import LlmCandidateReviewer, LlmPickWriter, LlmPostmortemAnalyst

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Runtime:
    """The wired pipeline and what must be closed with it."""

    ctx: PipelineContext
    matches: MatchFeed | None = None


def _scrub_for_call_log(text: str) -> str:
    """Mask credentials in a logged request or answer, keeping all of its length."""
    return scrub_text(text, limit=len(text))


def build_reviewer(
    llm: LlmService, sessions: async_sessionmaker[AsyncSession], clock: Clock, mode: LearningMode
) -> LlmCandidateReviewer:
    """The candidate reviewer.

    Learned context (lessons, segment statistics, notes) reaches the LLM only in ``apply`` mode
    (decision #20), so shadow and off keep the raw review.
    """
    experience = DbExperience(sessions, clock) if mode is LearningMode.APPLY else None
    return LlmCandidateReviewer(llm, experience=experience)


async def _promote_on_start(ctx: PipelineContext) -> None:
    """Journal shadow-time state as applied the moment the mode is ``apply`` (decision #23).

    Waiting for the weekly job would leave the stored state effective but unlisted for days.
    A database that is not ready yet is not fatal here: the weekly job promotes as well.
    """
    if ctx.learning_settings.mode is not LearningMode.APPLY:
        return
    try:
        await promote_shadow_state(ctx)
    except SQLAlchemyError:
        logger.exception("learning_promotion_failed")


@asynccontextmanager
async def open_match_feed(
    settings: Settings, recorder: CallRecorder | None = None
) -> AsyncIterator[MatchFeed | None]:
    """The live Football-Data.org feed, or ``None`` without ``FOOTBALL_DATA_ORG_KEY``.

    ``recorder`` stores every request in the call log (the only trace kept in the database).
    """
    key = settings.football_data_org_key
    if key is None or not key.get_secret_value().strip():
        logger.warning("matches_without_provider", reason="FOOTBALL_DATA_ORG_KEY is not set")
        yield None
        return
    client, http = create_football_data_client(
        key.get_secret_value(),
        requests_per_minute=settings.football_data_requests_per_minute,
        recorder=recorder,
    )
    try:
        yield MatchFeed(client)
    finally:
        await http.aclose()


@dataclass(frozen=True, slots=True)
class _FootballData:
    feed: MatchFeed
    provider: FootballDataProvider
    http: httpx.AsyncClient


def _football_data(
    settings: Settings, recorder: CallRecorder, store: SqlUsageStore, clock: Clock
) -> _FootballData | None:
    """The shared Football-Data.org client behind the match feed and the provider."""
    key = settings.football_data_org_key
    if key is None or not key.get_secret_value().strip():
        logger.warning("pipeline_without_provider", reason="FOOTBALL_DATA_ORG_KEY is not set")
        return None
    per_minute = settings.football_data_requests_per_minute
    quota = usage_counter(store, per_minute)
    client, http = create_football_data_client(
        key.get_secret_value(), requests_per_minute=per_minute, recorder=recorder, quota=quota
    )
    provider = FootballDataProvider(client, quota=quota, clock=clock)
    return _FootballData(feed=MatchFeed(client), provider=provider, http=http)


@asynccontextmanager
async def open_runtime(
    settings: Settings, *, clock: Clock = system_clock, config: PipelineConfig | None = None
) -> AsyncIterator[Runtime]:
    """Build the context from ``settings`` and release its resources on exit."""
    config = config or PipelineConfig(
        staking=StakingMode(settings.staking_mode),
        extra_bookmakers=settings.ingest_extra_bookmakers,
        call_log_retention=timedelta(days=settings.call_log_retention_days),
        display_zone=settings.display_timezone,
        refresh_at=settings.refresh_at,
        manual_refresh_min_interval=timedelta(minutes=settings.manual_refresh_min_minutes),
        # Football-Data.org's averaged 1X2 is the only price there is (ADR 0025).
        bet_bookmaker=BOOKMAKER_MARKET_AVERAGE,
    )
    engine = create_engine_from_settings(settings)
    await Ledger.ensure_schema(engine)
    sessions = create_session_factory(engine)
    # The call log writes on the request path, so it waits for the database lock only briefly.
    log_engine = create_usage_engine(settings.db_path)
    log_sessions = create_session_factory(log_engine)
    recorder = SqlCallRecorder(log_sessions, scrub=_scrub_for_call_log)
    football = _football_data(settings, recorder, SqlUsageStore(log_sessions), clock)
    providers: dict[Sport, SportProvider] = (
        {} if football is None else {football.provider.sport: football.provider}
    )
    # One service: reviews and post-mortems share its budget.
    llm = create_llm_service(settings, recorder=recorder)
    learning_settings = LearningSettings(
        mode=LearningMode(settings.learning_mode),
        manual_hold=timedelta(days=settings.learning_manual_hold_days),
    )
    # Only "apply" lets the learned rules reach the analysis; shadow keeps the raw model.
    learning: LearningSource = (
        DbLearningSource(sessions) if learning_settings.mode is LearningMode.APPLY else NoLearning()
    )
    ctx = PipelineContext(
        sessions=sessions,
        ledger=Ledger(sessions),
        providers=providers,
        clock=clock,
        config=config,
        reviewer=build_reviewer(llm, sessions, clock, learning_settings.mode),
        learning=learning,
        learning_settings=learning_settings,
        postmortem=LlmPostmortemAnalyst(llm),
        pick_writer=LlmPickWriter(llm),
    )
    try:
        await _promote_on_start(ctx)
        yield Runtime(ctx=ctx, matches=None if football is None else football.feed)
    finally:
        if football is not None:
            await football.http.aclose()
        await log_engine.dispose()
        await engine.dispose()
