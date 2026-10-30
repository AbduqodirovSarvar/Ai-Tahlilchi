"""Shared wiring of every pipeline job: database, providers, clock, configuration.

Jobs receive one :class:`PipelineContext` instead of a long argument list. It is immutable;
the only state behind it (the model cache, the ledger) is safe to share between jobs.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain import Sport
from app.pipeline.clock import Clock, system_clock
from app.pipeline.config import PipelineConfig
from app.pipeline.leagues import LeagueDirectory
from app.pipeline.learning_settings import LearningSettings
from app.pipeline.ledger import Ledger
from app.pipeline.modeling import ModelCache
from app.pipeline.overlay import LearningSource, NoLearning
from app.pipeline.provider import SportProvider
from app.pipeline.review import (
    AnalysisWriter,
    CandidateReviewer,
    NoAnalysisWriter,
    NoPostmortem,
    NoReview,
    PostmortemAnalyst,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class PipelineContext:
    """Everything a pipeline job needs, injected once.

    ``providers`` may be empty (database-only mode: no key configured); jobs then skip
    every step that needs the provider instead of failing.
    """

    sessions: async_sessionmaker[AsyncSession]
    ledger: Ledger
    providers: Mapping[Sport, SportProvider] = field(default_factory=dict)
    clock: Clock = system_clock
    config: PipelineConfig = field(default_factory=PipelineConfig)
    leagues: LeagueDirectory = field(default_factory=LeagueDirectory)
    reviewer: CandidateReviewer = field(default_factory=NoReview)
    learning: LearningSource = field(default_factory=NoLearning)
    postmortem: PostmortemAnalyst = field(default_factory=NoPostmortem)
    analysis_writer: AnalysisWriter = field(default_factory=NoAnalysisWriter)
    learning_settings: LearningSettings = field(default_factory=LearningSettings)
    models: ModelCache = field(init=False)

    def __post_init__(self) -> None:
        """Build the model cache over this context's database and league directory."""
        object.__setattr__(self, "models", ModelCache(self.sessions, self.leagues, self.config))
