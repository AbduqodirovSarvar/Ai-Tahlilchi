"""The contract view of the analyst's picks (:mod:`app.pipeline.analyst`, v1.16)."""

from collections.abc import Mapping, Sequence

from app.pipeline import forecast as pipeline_forecast
from app.pipeline import match_facts as pipeline_facts
from app.pipeline.analyst import AnalystPick, MatchAnalysis, PickOption
from app.services.interfaces import (
    ConfidenceLevel,
    ExpertMatch,
    ExpertPick,
    ExpertReport,
    MatchFacts,
    MatchItem,
    Meeting,
    OutcomeChance,
    TeamChance,
    TeamFacts,
    TeamPick,
    TopPick,
    VenueRecord,
)


def expert_report(
    items: Sequence[MatchItem], analyses: Mapping[int, MatchAnalysis]
) -> ExpertReport:
    """Matches with picks in the order of ``items``; the rest are counted, not listed."""
    matches = []
    for item in items:
        analysis = analyses.get(item.match_id)
        if analysis is not None and analysis.picks:
            matches.append(expert_match(item, analysis))
    return ExpertReport(matches=tuple(matches), without_forecast=len(items) - len(matches))


def expert_match(item: MatchItem, analysis: MatchAnalysis) -> ExpertMatch:
    """One analysed match (it must have picks)."""
    level = analysis.forecast.confidence
    confidence = None if level is None else ConfidenceLevel(level.value)
    return ExpertMatch(
        match=item,
        picks=tuple(_pick(item, pick, confidence) for pick in analysis.picks),
        facts=None if analysis.facts is None else match_facts(analysis.facts),
        by_ai=analysis.by_writer,
    )


def _pick(item: MatchItem, pick: AnalystPick, confidence: ConfidenceLevel | None) -> ExpertPick:
    return ExpertPick(pick=top_pick(item, pick.option, confidence), reason=pick.reason)


def top_pick(item: MatchItem, option: PickOption, confidence: ConfidenceLevel | None) -> TopPick:
    """The contract pick of one analyst option."""
    chance = option.chance
    if isinstance(chance, pipeline_forecast.TeamChance):
        team = TeamChance(pick=TeamPick(chance.pick.value), probability=_unit(chance.probability))
        return TopPick(match=item, team=team, confidence=confidence)
    outcome = OutcomeChance(
        market=chance.market,
        selection=chance.selection,
        probability=_unit(chance.probability),
        push=_unit(chance.push),
    )
    return TopPick(match=item, outcome=outcome, confidence=confidence)


def match_facts(facts: pipeline_facts.MatchFacts) -> MatchFacts:
    """The contract view of the history facts."""
    return MatchFacts(
        home=_team(facts.home),
        away=_team(facts.away),
        meetings=tuple(
            Meeting(
                day=m.day,
                home=m.home,
                away=m.away,
                home_goals=m.home_goals,
                away_goals=m.away_goals,
            )
            for m in facts.meetings
        ),
    )


def _team(facts: pipeline_facts.TeamFacts) -> TeamFacts:
    venue = facts.venue
    return TeamFacts(
        form=facts.form,
        venue=VenueRecord(
            played=venue.played,
            wins=venue.wins,
            draws=venue.draws,
            losses=venue.losses,
            goals_for=venue.goals_for,
            goals_against=venue.goals_against,
        ),
        goals_for_avg=facts.goals_for_avg,
        goals_against_avg=facts.goals_against_avg,
        over_2_5_share=facts.over_2_5_share,
        btts_share=facts.btts_share,
        sample=facts.sample,
        position=facts.position,
        points=facts.points,
    )


def _unit(probability: float) -> float:
    return min(max(probability, 0.0), 1.0)
