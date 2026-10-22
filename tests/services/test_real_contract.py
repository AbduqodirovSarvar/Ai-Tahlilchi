"""Runs the reusable contract suite against the real, pipeline-backed services."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.services.interfaces import Services
from tests.pipeline.harness import Env, open_env
from tests.pipeline.world import World
from tests.services.contract_suite import (
    AnalysisContract,
    BetContract,
    CatalogContract,
    ExportBetsContract,
    ExpressContract,
    FixtureDetailContract,
    MutableClock,
    RecalcContract,
    RecommendationContract,
    ReportContract,
    ResultContract,
    SettingsContract,
    SignalContract,
    SportsContract,
    StatusContract,
    TemplateContract,
    WatchlistContract,
)


@pytest.fixture
async def env(seeded_template: tuple[Path, World], tmp_path: Path) -> AsyncIterator[Env]:
    template, world = seeded_template
    async with open_env(template, world, tmp_path / "real.db") as opened:
        yield opened


@pytest.fixture
def clock(env: Env) -> MutableClock:
    return env.clock


@pytest.fixture
def services(env: Env) -> Services:
    return env.system.services


@pytest.fixture
def user_id() -> int:
    return 1001


class TestAnalysis(AnalysisContract):
    pass


class TestRecalc(RecalcContract):
    pass


class TestFixtureDetail(FixtureDetailContract):
    pass


class TestSignals(SignalContract):
    pass


class TestCatalog(CatalogContract):
    pass


class TestSettings(SettingsContract):
    pass


class TestTemplates(TemplateContract):
    pass


class TestWatchlist(WatchlistContract):
    pass


class TestBets(BetContract):
    pass


class TestResults(ResultContract):
    pass


class TestReports(ReportContract):
    pass


class TestStatus(StatusContract):
    pass


class TestRecommendations(RecommendationContract):
    pass


class TestSports(SportsContract):
    pass


class TestExportBets(ExportBetsContract):
    pass


class TestExpress(ExpressContract):
    pass
