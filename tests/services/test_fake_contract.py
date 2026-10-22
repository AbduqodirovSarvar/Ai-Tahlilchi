"""Runs the reusable contract suite against the in-memory fake services."""

import pytest

from app.services.fake import create_fake_services
from app.services.interfaces import Services
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
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def services(clock: MutableClock) -> Services:
    return create_fake_services(clock=clock)


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
