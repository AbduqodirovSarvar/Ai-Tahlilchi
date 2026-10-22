"""Production wiring of the pipeline: engine, ledger, provider, LLM reviewer.

``open_runtime`` is an async context manager that owns the database engine and the provider's
HTTP client and closes them on exit. Without ``API_FOOTBALL_KEY`` the runtime is
database-only (no provider): analyses of stored data still work, anything that needs the
provider is skipped or refused with a clear error.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

from app.clients.api_football import create_api_football_client
from app.config import Settings
from app.db.api_usage import SqlUsageStore
from app.db.engine import create_engine_from_settings, create_session_factory
from app.llm.service import create_llm_service
from app.logging import get_logger
from app.pipeline.clock import Clock, system_clock
from app.pipeline.config import PipelineConfig
from app.pipeline.context import PipelineContext
from app.pipeline.ledger import Ledger
from app.pipeline.provider import FootballProvider
from app.pipeline.review import LlmCandidateReviewer
from app.sports.football_mapping import HandicapConvention

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Runtime:
    """The wired pipeline and what must be closed with it."""

    ctx: PipelineContext
    http: httpx.AsyncClient | None


@asynccontextmanager
async def open_runtime(
    settings: Settings, *, clock: Clock = system_clock, config: PipelineConfig | None = None
) -> AsyncIterator[Runtime]:
    """Build the context from ``settings`` and release its resources on exit."""
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
        )
        providers[provider.sport] = provider
    else:
        logger.warning("pipeline_without_provider", reason="API_FOOTBALL_KEY is not set")
    ctx = PipelineContext(
        sessions=sessions,
        ledger=Ledger(sessions),
        providers=providers,
        clock=clock,
        config=config or PipelineConfig(),
        reviewer=LlmCandidateReviewer(create_llm_service(settings)),
    )
    try:
        yield Runtime(ctx=ctx, http=http)
    finally:
        if http is not None:
            await http.aclose()
        await engine.dispose()
