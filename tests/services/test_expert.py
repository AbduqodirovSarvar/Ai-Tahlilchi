"""Expert picks (v1.16): risk bands, the contract mapping and both implementations."""

from datetime import UTC, date, datetime

import pytest

from app.domain import Market, Selection
from app.models import ConfidenceLevel as ModelConfidence
from app.pipeline.analyst import AnalystPick, Family, MatchAnalysis, PickOption
from app.pipeline.forecast import Chance, Forecast, ForecastSkip
from app.pipeline.forecast import TeamChance as PipelineTeamChance
from app.pipeline.forecast import TeamPick as PipelineTeamPick
from app.pipeline.match_facts import MatchFacts as PipelineFacts
from app.pipeline.match_facts import Meeting as PipelineMeeting
from app.pipeline.match_facts import Record
from app.pipeline.match_facts import TeamFacts as PipelineTeamFacts
from app.pipeline.matches import Match
from app.services.errors import ValidationFailedError
from app.services.fake import create_fake_services
from app.services.interfaces import (
    ConfidenceLevel,
    ExpertMatch,
    ExpertPick,
    MatchItem,
    MatchState,
    RiskLevel,
    TeamChance,
    TeamPick,
    TopPick,
    risk_level,
)
from app.services.real.expert import expert_report
from app.services.real.matches import RealMatchesService
from tests.services.test_matches import StubFeed, provider_match

DAY = date(2026, 10, 30)


@pytest.mark.parametrize(
    ("probability", "level"),
    [
        (0.99, RiskLevel.SAFE),
        (0.70, RiskLevel.SAFE),
        (0.6999, RiskLevel.OPTIMAL),
        (0.55, RiskLevel.OPTIMAL),
        (0.54, RiskLevel.RISKY),
        (0.40, RiskLevel.RISKY),
        (0.39, RiskLevel.LONG_SHOT),
    ],
)
def test_risk_bands(probability: float, level: RiskLevel) -> None:
    assert risk_level(probability) is level


def item(match_id: int) -> MatchItem:
    return MatchItem(
        match_id=match_id,
        kickoff=datetime(2026, 10, 30, 16, tzinfo=UTC),
        competition="Premier League",
        competition_code="PL",
        home="Arsenal FC",
        away="Chelsea FC",
        state=MatchState.SCHEDULED,
    )


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


def analysis(match_id: int, *picks: AnalystPick, skip: ForecastSkip | None = None) -> MatchAnalysis:
    return MatchAnalysis(
        match_id=match_id,
        forecast=Forecast(match_id=match_id, skip=skip, confidence=ModelConfidence.HIGH),
        facts=PipelineFacts(
            home=team_facts("WWDLW"),
            away=team_facts("LLDWL"),
            meetings=(PipelineMeeting(DAY, "Arsenal", "Chelsea", 2, 1),),
        ),
        picks=picks,
        by_writer=bool(picks and picks[0].reason),
    )


HANDICAP = PickOption(
    "asian_handicap+1.home",
    Chance(Market.asian_handicap(1.0), Selection.HOME, 0.74, push=0.15),
    Family.RESULT,
)
SCORES = PickOption(
    "team.away_over_1_5", PipelineTeamChance(PipelineTeamPick.AWAY_OVER_1_5, 0.41), Family.GOALS
)


def test_report_maps_picks_facts_and_counts_the_rest() -> None:
    analyses = {
        1: analysis(1, AnalystPick(HANDICAP, "Arsenal uyda kuchli."), AnalystPick(SCORES)),
        2: analysis(2, skip=ForecastSkip.THIN_HISTORY),
    }
    report = expert_report([item(1), item(2), item(3)], analyses)
    assert report.without_forecast == 2
    [expert] = report.matches
    handicap, scores = expert.picks
    assert handicap.reason == "Arsenal uyda kuchli."
    assert handicap.risk is RiskLevel.SAFE
    assert handicap.pick.outcome is not None
    assert handicap.pick.outcome.push == pytest.approx(0.15)
    assert handicap.pick.confidence is ConfidenceLevel.HIGH
    assert scores.reason is None
    assert scores.pick.team is not None
    assert scores.pick.team.pick is TeamPick.AWAY_OVER_1_5
    assert expert.by_ai
    assert expert.facts is not None
    assert expert.facts.home.venue.wins == 3
    assert expert.facts.meetings[0].home_goals == 2


def test_expert_match_needs_one_or_two_picks() -> None:
    chance = TeamChance(pick=TeamPick.HOME_TO_SCORE, probability=0.6)
    pick = ExpertPick(pick=TopPick(match=item(1), team=chance))
    with pytest.raises(ValidationFailedError):
        ExpertMatch(match=item(1), picks=())
    with pytest.raises(ValidationFailedError):
        ExpertMatch(match=item(1), picks=(pick, pick, pick))
    assert not ExpertMatch(match=item(1), picks=(pick,)).by_ai
    assert pick.risk is RiskLevel.OPTIMAL


class StubAnalyst:
    def __init__(self) -> None:
        self.asked: list[list[int]] = []

    async def analyse(self, matches: list[Match]) -> dict[int, MatchAnalysis]:
        self.asked.append([m.id for m in matches])
        return {m.id: analysis(m.id, AnalystPick(HANDICAP)) for m in matches}


async def test_real_service_analyses_only_matches_still_to_play() -> None:
    upcoming = provider_match("TIMED")
    finished = provider_match("FINISHED", id=2)
    analyst = StubAnalyst()
    settings = create_fake_services().settings
    service = RealMatchesService(StubFeed([upcoming, finished]), settings, None, analyst)  # type: ignore[arg-type]
    report = await service.expert_picks(1, DAY, DAY)
    assert analyst.asked == [[upcoming.id]]
    assert [m.match.match_id for m in report.matches] == [upcoming.id]
    without = RealMatchesService(StubFeed([upcoming]), settings)  # type: ignore[arg-type]
    assert (await without.expert_picks(1, DAY, DAY)).without_forecast == 1
    with pytest.raises(ValidationFailedError):
        await service.expert_picks(1, DAY, date(2026, 12, 30))


async def test_fake_expert_picks() -> None:
    services = create_fake_services()
    report = await services.matches.expert_picks(1, DAY, DAY)
    assert report.matches
    first = report.matches[0]
    assert first.by_ai
    assert first.picks[0].reason
    assert all(1 <= len(m.picks) <= 2 for m in report.matches)
    assert all(m.match.state is MatchState.SCHEDULED for m in report.matches)
