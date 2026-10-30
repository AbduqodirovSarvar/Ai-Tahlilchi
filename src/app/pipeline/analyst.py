"""The analyst: a deep, neutral study of one match (ADR 0029).

No odds, no EV, no picks: the user asks about a match and gets its analysis. The numbers are
the pipeline's own: the model forecast (:mod:`app.pipeline.forecast`, background only) and the
history facts (:mod:`app.pipeline.match_facts`); line-ups come from the provider. The analysis
writer (the LLM) words the five sections from exactly these inputs. Without a writer, or when it
gives no answer, the caller words the sections from the facts, so a study is never empty.
"""

from dataclasses import dataclass
from typing import Final

from app.domain import Market, Selection
from app.llm.analysis import (
    MAX_MEETINGS,
    MAX_PLAYERS,
    AnalysisText,
    MatchAnalysisInput,
    OutlookInput,
    SquadInput,
    TeamFactsInput,
)
from app.logging import get_logger
from app.pipeline.context import PipelineContext
from app.pipeline.forecast import Forecast, Forecaster, chances_of
from app.pipeline.match_facts import MatchFacts, TeamFacts, read_facts
from app.pipeline.matches import Match, MatchTeam

logger = get_logger(__name__)

TEXT_LIMIT: Final = 80
"""Length cap of a name or line in the writer's input (its schema rejects longer ones)."""


@dataclass(frozen=True, slots=True)
class MatchStudy:
    """What is known about a match: forecast, facts and the writer's sections (if any)."""

    match: Match
    forecast: Forecast
    facts: MatchFacts | None = None
    text: AnalysisText | None = None


class Analyst:
    """Forecast, gather facts, then let the writer word the analysis."""

    def __init__(self, ctx: PipelineContext, forecaster: Forecaster | None = None) -> None:
        """Read the history through ``ctx``; ``ctx.analysis_writer`` writes."""
        self._ctx = ctx
        self._forecaster = forecaster or Forecaster(ctx)

    async def study(self, match: Match) -> MatchStudy:
        """The study of ``match`` (facts and forecast are absent for an untracked league)."""
        forecast = (await self._forecaster.forecast([match]))[match.id]
        facts: MatchFacts | None = None
        if forecast.fixture is not None:
            facts = (await read_facts(self._ctx, {match.id: forecast.fixture})).get(match.id)
        text = await self._ctx.analysis_writer.write(writer_input(match, forecast, facts))
        logger.info(
            "analyst_done",
            match_id=match.id,
            forecast=forecast.skip is None,
            facts=facts is not None,
            sections=0 if text is None else text.count(),
        )
        return MatchStudy(match=match, forecast=forecast, facts=facts, text=text)


def writer_input(match: Match, forecast: Forecast, facts: MatchFacts | None) -> MatchAnalysisInput:
    """The writer's view of one match (English keys and codes: the reader is the LLM)."""
    home = _short(match.home_team.name or "Home")
    away = _short(match.away_team.name or "Away")
    return MatchAnalysisInput(
        league=_short((match.competition.name if match.competition else None) or "-"),
        kickoff=match.utc_date.strftime("%Y-%m-%d %H:%M UTC"),
        home_team=home,
        away_team=away,
        home=None if facts is None else _team_input(home, facts.home, at_home=True),
        away=None if facts is None else _team_input(away, facts.away, at_home=False),
        meetings=()
        if facts is None
        else tuple(
            _short(f"{m.day:%Y-%m-%d} {m.home} {m.home_goals}-{m.away_goals} {m.away}")
            for m in facts.meetings[:MAX_MEETINGS]
        ),
        outlook=outlook_input(forecast),
        home_squad=squad_input(match.home_team),
        away_squad=squad_input(match.away_team),
    )


def outlook_input(forecast: Forecast) -> OutlookInput | None:
    """The model's main probabilities, or ``None`` without a forecast."""
    if forecast.skip is not None or forecast.expected_goals is None:
        return None
    result = chances_of(forecast, Market.match_result())
    totals = chances_of(forecast, Market.totals(2.5))
    btts = chances_of(forecast, Market.btts())
    home_goals, away_goals = forecast.expected_goals
    return OutlookInput(
        home_win=_unit(result.get(Selection.HOME, 0.0)),
        draw=_unit(result.get(Selection.DRAW, 0.0)),
        away_win=_unit(result.get(Selection.AWAY, 0.0)),
        over_2_5=_unit(totals.get(Selection.OVER, 0.0)),
        btts=_unit(btts.get(Selection.YES, 0.0)),
        expected_home_goals=max(home_goals, 0.0),
        expected_away_goals=max(away_goals, 0.0),
    )


def squad_input(team: MatchTeam) -> SquadInput | None:
    """An announced line-up; ``None`` until the provider lists the starters."""
    starters = tuple(_short(p.name) for p in team.lineup if p.name)[:MAX_PLAYERS]
    if not starters:
        return None
    return SquadInput(
        formation=_short(team.formation) if team.formation else None,
        coach=_short(team.coach.name) if team.coach and team.coach.name else None,
        starters=starters,
    )


def _team_input(name: str, facts: TeamFacts, *, at_home: bool) -> TeamFactsInput:
    venue = facts.venue
    where = "home" if at_home else "away"
    return TeamFactsInput(
        team=name,
        form=facts.form,
        venue_record=(
            f"{where} last {venue.played}: {venue.wins}W {venue.draws}D {venue.losses}L, "
            f"goals {venue.goals_for}:{venue.goals_against}"
        ),
        goals_for_avg=facts.goals_for_avg,
        goals_against_avg=facts.goals_against_avg,
        over_2_5_share=facts.over_2_5_share,
        btts_share=facts.btts_share,
        position=facts.position,
        points=facts.points,
    )


def _short(text: str) -> str:
    return text.strip()[:TEXT_LIMIT] or "-"


def _unit(probability: float) -> float:
    return min(max(probability, 0.0), 1.0)
