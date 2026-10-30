"""The contract view of the analyst's study (:mod:`app.pipeline.analyst`, v1.16) and its cache."""

from collections.abc import Callable
from datetime import datetime, timedelta

from app.pipeline import match_facts as pipeline_facts
from app.pipeline.analyst import MatchStudy, outlook_input
from app.services.interfaces import (
    ANALYSIS_REFRESH_SECONDS,
    ANALYSIS_TTL_SECONDS,
    AnalysisSections,
    MatchAnalysis,
    MatchFacts,
    MatchOutlook,
    MatchSheet,
    Meeting,
    TeamFacts,
    VenueRecord,
)

type Clock = Callable[[], datetime]


def match_analysis(sheet: MatchSheet, study: MatchStudy, generated_at: datetime) -> MatchAnalysis:
    """The analysis of ``study`` over the provider's ``sheet`` of the same match."""
    outlook = outlook_input(study.forecast)
    return MatchAnalysis(
        sheet=sheet,
        facts=None if study.facts is None else match_facts(study.facts),
        outlook=None
        if outlook is None
        else MatchOutlook(
            home_win=outlook.home_win,
            draw=outlook.draw,
            away_win=outlook.away_win,
            over_2_5=outlook.over_2_5,
            btts=outlook.btts,
            expected_home_goals=outlook.expected_home_goals,
            expected_away_goals=outlook.expected_away_goals,
        ),
        sections=_sections(study),
        generated_at=generated_at,
    )


def _sections(study: MatchStudy) -> AnalysisSections | None:
    text = study.text
    if text is None:
        return None
    return AnalysisSections(
        form=text.form,
        venue=text.venue,
        squads=text.squads,
        tactics=text.tactics,
        summary=text.summary,
    )


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


class AnalysisCache:
    """Recent analyses by match id: one AI request per match per window, not per tap.

    Process memory only: a restart costs one request per match, which is cheaper than a table.
    """

    def __init__(self, clock: Clock) -> None:
        """Start empty; ``clock`` gives the current UTC instant."""
        self.clock = clock
        self._items: dict[int, MatchAnalysis] = {}

    def get(self, match_id: int, *, refresh: bool) -> MatchAnalysis | None:
        """A reusable analysis: younger than the TTL, or than the refresh floor on refresh."""
        cached = self._items.get(match_id)
        if cached is None:
            return None
        limit = ANALYSIS_REFRESH_SECONDS if refresh else ANALYSIS_TTL_SECONDS
        if self.clock() - cached.generated_at < timedelta(seconds=limit):
            return cached
        return None

    def put(self, analysis: MatchAnalysis) -> None:
        """Keep ``analysis``; stale entries go so the cache never outgrows a few days."""
        now = self.clock()
        ttl = timedelta(seconds=ANALYSIS_TTL_SECONDS)
        self._items = {
            match_id: item
            for match_id, item in self._items.items()
            if now - item.generated_at < ttl
        }
        self._items[analysis.sheet.match.match_id] = analysis
