"""The analyst: 1-2 picks for every forecastable match, with the model's probability and a reason.

No odds and no EV filter: every match with a forecast gets at least one pick. The numbers are
the model's (:mod:`app.pipeline.forecast`); the pick writer (the LLM, ADR 0029) only chooses
among the model's options and explains the choice from the history facts
(:mod:`app.pipeline.match_facts`). When the writer gives no answer for a match, the rule of
:func:`rule_picks` chooses and the reason is left to the caller to word from the facts.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.domain import Market, MarketType, Selection
from app.llm.picks import (
    MAX_MEETINGS,
    MAX_OPTIONS_PER_MATCH,
    MAX_PICKS_PER_MATCH,
    MatchPicksInput,
    PickChoice,
    PickOptionInput,
    TeamFactsInput,
)
from app.logging import get_logger
from app.pipeline.context import PipelineContext
from app.pipeline.forecast import Chance, Forecast, Forecaster, TeamChance
from app.pipeline.match_facts import MatchFacts, TeamFacts, read_facts
from app.pipeline.matches import Match

logger = get_logger(__name__)

SECOND_PICK_FLOOR: Final = 0.40
"""A second pick below this would be a coin toss: one pick is enough then."""
OPTIONS_PER_FAMILY: Final = MAX_OPTIONS_PER_MATCH // 2


class Family(StrEnum):
    """What a selection is about: two picks never come from the same family."""

    RESULT = "result"  # 1X2, double chance, handicap
    GOALS = "goals"  # totals, both teams to score, team totals


@dataclass(frozen=True, slots=True)
class PickOption:
    """One priced selection a pick can be made of."""

    option_id: str
    chance: Chance | TeamChance
    family: Family

    @property
    def probability(self) -> float:
        """The model probability that the selection wins."""
        return self.chance.probability


@dataclass(frozen=True, slots=True)
class AnalystPick:
    """A chosen option; ``reason`` is the writer's, ``None`` when it is to be worded from facts."""

    option: PickOption
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class MatchAnalysis:
    """A match's forecast, facts and picks (no picks when the forecast was skipped)."""

    match_id: int
    forecast: Forecast
    facts: MatchFacts | None = None
    picks: tuple[AnalystPick, ...] = ()
    by_writer: bool = False


class Analyst:
    """Forecast, gather facts, then let the writer (or the rule) choose."""

    def __init__(self, ctx: PipelineContext, forecaster: Forecaster | None = None) -> None:
        """Read the history through ``ctx``; ``ctx.pick_writer`` chooses."""
        self._ctx = ctx
        self._forecaster = forecaster or Forecaster(ctx)

    async def analyse(self, matches: Sequence[Match]) -> dict[int, MatchAnalysis]:
        """An analysis for every match, keyed by provider match id."""
        forecasts = await self._forecaster.forecast(matches)
        ready = {
            match_id: forecast.fixture
            for match_id, forecast in forecasts.items()
            if forecast.skip is None and forecast.fixture is not None
        }
        facts = await read_facts(self._ctx, ready)
        options = {match_id: pick_options(forecasts[match_id]) for match_id in ready}
        inputs = [
            writer_input(match, facts[match.id], options[match.id])
            for match in matches
            if match.id in facts and options.get(match.id)
        ]
        written = await self._ctx.pick_writer.write(inputs) if inputs else {}
        analyses: dict[int, MatchAnalysis] = {}
        for match_id, forecast in forecasts.items():
            choices = written.get(str(match_id))
            found = options.get(match_id, ())
            analyses[match_id] = MatchAnalysis(
                match_id=match_id,
                forecast=forecast,
                facts=facts.get(match_id),
                picks=_chosen(found, choices) if choices else rule_picks(found),
                by_writer=bool(choices),
            )
        logger.info("analyst_done", matches=len(matches), ready=len(ready), by_writer=len(written))
        return analyses


def pick_options(forecast: Forecast) -> tuple[PickOption, ...]:
    """Every priced selection of ``forecast`` (cards excluded), most likely first."""
    options = [
        PickOption(_chance_id(chance), chance, _family(chance.market))
        for chance in forecast.chances
    ]
    options += [
        PickOption(f"team.{chance.pick.value}", chance, Family.GOALS)
        for chance in forecast.team_chances
    ]
    return tuple(sorted(options, key=lambda option: -option.probability))


def rule_picks(options: Sequence[PickOption]) -> tuple[AnalystPick, ...]:
    """The most likely option, plus the most likely of the other family if it is no coin toss."""
    if not options:
        return ()
    first = max(options, key=lambda option: option.probability)
    others = [option for option in options if option.family is not first.family]
    second = max(others, key=lambda option: option.probability, default=None)
    if second is None or second.probability < SECOND_PICK_FLOOR:
        return (AnalystPick(first),)
    return AnalystPick(first), AnalystPick(second)


def writer_options(options: Sequence[PickOption]) -> tuple[PickOption, ...]:
    """What the writer may choose from: the likeliest of each family, most likely first."""
    picked = [
        option
        for family in Family
        for option in [o for o in options if o.family is family][:OPTIONS_PER_FAMILY]
    ]
    return tuple(sorted(picked, key=lambda option: -option.probability))


def writer_input(match: Match, facts: MatchFacts, options: Sequence[PickOption]) -> MatchPicksInput:
    """The writer's view of one match (English labels: the reader is the LLM, not the user)."""
    home = match.home_team.name or "Home"
    away = match.away_team.name or "Away"
    return MatchPicksInput(
        match_id=str(match.id),
        league=(match.competition.name if match.competition else None) or "-",
        kickoff=match.utc_date.strftime("%Y-%m-%d %H:%M UTC"),
        home=_team_input(home, facts.home, at_home=True),
        away=_team_input(away, facts.away, at_home=False),
        meetings=tuple(
            f"{m.day:%Y-%m-%d} {m.home} {m.home_goals}-{m.away_goals} {m.away}"[:80]
            for m in facts.meetings[:MAX_MEETINGS]
        ),
        lineups_announced=bool(match.home_team.lineup and match.away_team.lineup),
        options=tuple(
            PickOptionInput(
                option_id=option.option_id,
                label=option_label(option, home, away),
                probability=min(max(option.probability, 0.0), 1.0),
            )
            for option in writer_options(options)
        ),
    )


def option_label(option: PickOption, home: str, away: str) -> str:
    """A plain English label of an option, e.g. ``Asian handicap -1: Arsenal``."""
    chance = option.chance
    if isinstance(chance, TeamChance):
        side = home if chance.pick.value.startswith("home") else away
        goals = "over 1.5 goals" if chance.pick.value.endswith("1_5") else "to score"
        return f"{side} {goals}"
    market, selection = chance.market, chance.selection
    names = {Selection.HOME: home, Selection.AWAY: away}
    match market.type:
        case MarketType.MATCH_RESULT:
            return "Draw" if selection is Selection.DRAW else f"{names[selection]} to win"
        case MarketType.DOUBLE_CHANCE:
            return {
                Selection.HOME_OR_DRAW: f"Double chance 1X ({home} not to lose)",
                Selection.DRAW_OR_AWAY: f"Double chance X2 ({away} not to lose)",
            }.get(selection, "Double chance 12 (no draw)")
        case MarketType.TOTALS:
            return f"Total goals {selection.value} {market.line}"
        case MarketType.BTTS:
            return f"Both teams to score: {selection.value}"
        case MarketType.ASIAN_HANDICAP:
            line = market.line or 0.0
            side_line = line if selection is Selection.HOME else -line
            return f"Asian handicap {side_line:+g}: {names[selection]}"


def _chosen(
    options: Sequence[PickOption], choices: Sequence[PickChoice]
) -> tuple[AnalystPick, ...]:
    by_id = {option.option_id: option for option in options}
    picks = [
        AnalystPick(by_id[choice.option_id], choice.reason)
        for choice in choices
        if choice.option_id in by_id
    ]
    return tuple(picks[:MAX_PICKS_PER_MATCH]) or rule_picks(options)


def _chance_id(chance: Chance) -> str:
    market = chance.market
    line = "" if market.line is None else f"{market.line:+g}"
    return f"{market.type.value}{line}.{chance.selection.value}"


def _family(market: Market) -> Family:
    if market.type in {MarketType.TOTALS, MarketType.BTTS}:
        return Family.GOALS
    return Family.RESULT


def _team_input(name: str, facts: TeamFacts, *, at_home: bool) -> TeamFactsInput:
    venue = facts.venue
    where = "home" if at_home else "away"
    return TeamFactsInput(
        team=name[:80],
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
