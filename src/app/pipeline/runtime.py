"""Production wiring of the pipeline: engine, ledger, provider, LLM reviewer.

``open_runtime`` is an async context manager that owns the database engine and the provider's
HTTP client and closes them on exit. Without ``API_FOOTBALL_KEY`` the runtime is
database-only (no provider): analyses of stored data still work, anything that needs the
provider is skipped or refused with a clear error.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta

import httpx
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.api_football import create_api_football_client
from app.config import Settings
from app.db.api_usage import SqlUsageStore
from app.db.engine import create_engine_from_settings, create_session_factory
from app.llm.service import LlmService, create_llm_service
from app.logging import get_logger
from app.pipeline.clock import Clock, system_clock
from app.pipeline.config import PipelineConfig, StakingMode
from app.pipeline.context import PipelineContext
from app.pipeline.experience import DbExperience
from app.pipeline.learning import promote_shadow_state
from app.pipeline.learning_settings import LearningMode, LearningSettings
from app.pipeline.ledger import Ledger
from app.pipeline.overlay import DbLearningSource, LearningSource, NoLearning
from app.pipeline.provider import FootballProvider
from app.pipeline.review import LlmCandidateReviewer, LlmPostmortemAnalyst
from app.sports.football_mapping import HandicapConvention

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Runtime:
    """The wired pipeline and what must be closed with it."""

    ctx: PipelineContext
    http: httpx.AsyncClient | None


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
async def open_runtime(
    settings: Settings, *, clock: Clock = system_clock, config: PipelineConfig | None = None
) -> AsyncIterator[Runtime]:
    """Build the context from ``settings`` and release its resources on exit."""
    config = config or PipelineConfig(staking=StakingMode(settings.staking_mode))
    engine = create_engine_from_settings(settings)
    await Ledger.ensure_schema(engine)
    sessions = create_session_factory(engine)
    http: httpx.AsyncClient | None = None
    providers = {}
    key = settings.api_football_key
    if key is not None and key.get_secret_value().strip():
        client, http = create_api_football_client(
            key.get_secret_value(),
            SqlUsageStore(sessions),
        )
        provider = FootballProvider(
            client,
            clock=clock,
            handicap_convention=HandicapConvention(settings.handicap_convention),
            bookmakers=config.ingest_bookmakers,
        )
        providers[provider.sport] = provider
    else:
        logger.warning("pipeline_without_provider", reason="API_FOOTBALL_KEY is not set")
    llm = create_llm_service(settings)  # one service: reviews and post-mortems share its budget
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
    )
    try:
        await _promote_on_start(ctx)
        yield Runtime(ctx=ctx, http=http)
    finally:
        if http is not None:
            await http.aclose()
        await engine.dispose()
