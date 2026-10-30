"""Match analysis (v1.16): the contract DTOs, the cache and both implementations."""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.llm.analysis import AnalysisText
from app.pipeline.analyst import MatchStudy
from app.pipeline.forecast import Forecast, ForecastSkip
from app.pipeline.match_facts import MatchFacts as PipelineFacts
from app.pipeline.match_facts import Meeting as PipelineMeeting
from app.pipeline.match_facts import Record
from app.pipeline.match_facts import TeamFacts as PipelineTeamFacts
from app.pipeline.matches import Match
from app.services.errors import ServiceUnavailableError, ValidationFailedError
from app.services.fake import create_fake_services
from app.services.interfaces import (
    ANALYSIS_REFRESH_SECONDS,
    ANALYSIS_TTL_SECONDS,
    AnalysisSections,
    MatchAnalysis,
    MatchOutlook,
)
from app.services.real.match_analysis import AnalysisCache, match_analysis
from app.services.real.matches import RealMatchesService, match_sheet
from tests.services.test_matches import StubFeed, provider_match

NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)
DAY = date(2026, 10, 30)


def team_facts(form: str) -> PipelineTeamFacts:
    return PipelineTeamFacts(
        form=form,
        venue=Record(played=5, wins=3, draws=1, losses=1, goals_for=9, goals_against=4),
        goals_for_avg=1.8,
        goals_against_avg=1.0,
        over_2_5_share=0.5,
        btts_share=0.4,
        sample=10,
        position=3,
        points=18,
    )


def study(match: Match, *, text: AnalysisText | None = None, ready: bool = True) -> MatchStudy:
    forecast = (
        Forecast(match_id=match.id, expected_goals=(1.5, 1.0))
        if ready
        else Forecast(match_id=match.id, skip=ForecastSkip.UNTRACKED_LEAGUE)
    )
    facts = (
        PipelineFacts(
            home=team_facts("WWDLW"),
            away=team_facts("LLDWL"),
            meetings=(PipelineMeeting(DAY, "Chelsea", "Liverpool", 2, 1),),
        )
        if ready
        else None
    )
    return MatchStudy(match=match, forecast=forecast, facts=facts, text=text)


class StubAnalyst:
    def __init__(self, text: AnalysisText | None = None) -> None:
        self.text = text
        self.asked: list[int] = []

    async def study(self, match: Match) -> MatchStudy:
        self.asked.append(match.id)
        return study(match, text=self.text)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def test_mapping_keeps_facts_outlook_and_sections() -> None:
    upcoming = provider_match("TIMED")
    text = AnalysisText(form="Forma yaxshi.", summary=None)
    analysis = match_analysis(match_sheet(upcoming), study(upcoming, text=text), NOW)
    assert analysis.facts is not None
    assert analysis.facts.home.venue.wins == 3
    assert analysis.facts.meetings[0].home_goals == 2
    assert analysis.outlook is not None
    assert analysis.outlook.expected_home_goals == 1.5
    assert analysis.sections == AnalysisSections(form="Forma yaxshi.")
    assert analysis.by_ai
    bare = match_analysis(match_sheet(upcoming), study(upcoming, ready=False), NOW)
    assert (bare.facts, bare.outlook, bare.sections, bare.by_ai) == (None, None, None, False)


def test_contract_checks() -> None:
    with pytest.raises(ValidationFailedError):
        MatchOutlook(
            home_win=1.2,
            draw=0,
            away_win=0,
            over_2_5=0,
            btts=0,
            expected_home_goals=1,
            expected_away_goals=1,
        )
    with pytest.raises(ValidationFailedError):
        MatchOutlook(
            home_win=0.5,
            draw=0.3,
            away_win=0.2,
            over_2_5=0.5,
            btts=0.5,
            expected_home_goals=-1,
            expected_away_goals=1,
        )
    sheet = match_sheet(provider_match("TIMED"))
    with pytest.raises(ValidationFailedError):
        MatchAnalysis(sheet=sheet, generated_at=datetime(2026, 10, 30, 12))  # noqa: DTZ001 - naive on purpose
    assert not MatchAnalysis(sheet=sheet, sections=AnalysisSections(), generated_at=NOW).by_ai


def test_cache_reuses_until_the_ttl_and_refresh_has_a_floor() -> None:
    clock = Clock()
    cache = AnalysisCache(clock)
    sheet = match_sheet(provider_match("TIMED"))
    analysis = MatchAnalysis(sheet=sheet, generated_at=NOW)
    assert cache.get(sheet.match.match_id, refresh=False) is None
    cache.put(analysis)
    clock.now = NOW + timedelta(seconds=ANALYSIS_REFRESH_SECONDS - 1)
    assert cache.get(sheet.match.match_id, refresh=True) is analysis  # tapping does not spend
    clock.now = NOW + timedelta(seconds=ANALYSIS_REFRESH_SECONDS)
    assert cache.get(sheet.match.match_id, refresh=True) is None
    assert cache.get(sheet.match.match_id, refresh=False) is analysis
    clock.now = NOW + timedelta(seconds=ANALYSIS_TTL_SECONDS)
    assert cache.get(sheet.match.match_id, refresh=False) is None
    other = MatchAnalysis(sheet=match_sheet(provider_match("TIMED", id=7)), generated_at=clock.now)
    cache.put(other)  # the stale entry is pruned on the way
    clock.now = NOW
    assert cache.get(sheet.match.match_id, refresh=False) is None


async def test_real_service_analyses_once_per_window() -> None:
    upcoming = provider_match("TIMED")
    analyst = StubAnalyst(AnalysisText(summary="Teng o'yin kutilmoqda."))
    clock = Clock()
    settings = create_fake_services().settings
    service = RealMatchesService(StubFeed([upcoming]), settings, None, analyst, clock=clock)  # type: ignore[arg-type]
    first = await service.analyse_match(1, upcoming.id)
    assert first.sheet.match.match_id == upcoming.id
    assert first.sheet.home.coach == "Enzo Maresca"
    assert first.generated_at == NOW
    assert first.by_ai
    assert await service.analyse_match(1, upcoming.id, refresh=True) is first
    clock.now = NOW + timedelta(seconds=ANALYSIS_REFRESH_SECONDS)
    fresh = await service.analyse_match(1, upcoming.id, refresh=True)
    assert fresh.generated_at == clock.now
    assert analyst.asked == [upcoming.id, upcoming.id]


async def test_real_service_without_an_analyst_or_a_provider() -> None:
    upcoming = provider_match("TIMED")
    settings = create_fake_services().settings
    bare = RealMatchesService(StubFeed([upcoming]), settings)  # type: ignore[arg-type]
    analysis = await bare.analyse_match(1, upcoming.id)
    assert (analysis.facts, analysis.outlook, analysis.sections) == (None, None, None)
    with pytest.raises(ServiceUnavailableError):
        await RealMatchesService(None, settings).analyse_match(1, upcoming.id)


async def test_fake_analysis_has_ai_and_fact_samples() -> None:
    services = create_fake_services()
    [first, second] = await services.matches.list_matches(1, DAY, DAY)
    with_ai = await services.matches.analyse_match(1, first.match_id)
    worded = await services.matches.analyse_match(1, second.match_id, refresh=True)
    assert with_ai.by_ai
    assert with_ai.facts is not None
    assert with_ai.outlook is not None
    assert not worded.by_ai
