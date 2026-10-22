"""Recent form of a team from the stored history.

The one definition, shared by the detail card and the LLM input.

Form is the team's last :data:`FORM_LENGTH` finished matches of its league, oldest first. Only
matches that were over before the fixture in question count, so a signal's LLM input never
contains what the match itself decided.
"""

from collections import defaultdict
from collections.abc import Sequence
from datetime import timedelta
from typing import Final

from app.db.repo import FixtureRecord, finished_fixtures
from app.domain import ExternalRef, Team
from app.llm.inputs import TeamForm
from app.pipeline.context import PipelineContext

FORM_LENGTH: Final = 5
FORM_LOOKBACK: Final = timedelta(days=120)
"""How far back the history is read: wide enough for five matches even after a break."""
_MAX_NAME_CHARS: Final = 80


def score_of(record: FixtureRecord) -> tuple[int, int] | None:
    """``(home, away)`` goals of a finished record, ``None`` without a score."""
    result = record.result
    if result is None or result.home_score is None or result.away_score is None:
        return None
    return result.home_score, result.away_score


def team_results(
    team: Team, history: Sequence[FixtureRecord], *, limit: int = FORM_LENGTH
) -> list[tuple[int, int]]:
    """``(goals for, goals against)`` of the team's last ``limit`` matches in ``history``.

    ``history`` is ordered oldest first (as ``finished_fixtures`` returns it); so is the answer.
    """
    results = []
    for record in history:
        score = score_of(record)
        if score is None or team.ref not in (record.fixture.home.ref, record.fixture.away.ref):
            continue
        results.append(score if record.fixture.home.ref == team.ref else score[::-1])
    return results[-limit:]


def result_letter(goals_for: int, goals_against: int) -> str:
    """``"W"``, ``"D"`` or ``"L"``."""
    if goals_for > goals_against:
        return "W"
    return "D" if goals_for == goals_against else "L"


def team_form(team: Team, history: Sequence[FixtureRecord]) -> TeamForm | None:
    """The compact form of ``team`` for the LLM, ``None`` when it has no matches in ``history``."""
    results = team_results(team, history)
    if not results or not team.name.strip() or len(team.name) > _MAX_NAME_CHARS:
        return None
    return TeamForm(
        team=team.name,
        results="".join(result_letter(*pair) for pair in results),
        goals_for=sum(pair[0] for pair in results),
        goals_against=sum(pair[1] for pair in results),
    )


async def forms_for(
    ctx: PipelineContext, records: Sequence[FixtureRecord]
) -> dict[int, tuple[TeamForm, ...]]:
    """``fixture id -> (home form, away form)`` for ``records`` (teams without matches left out).

    The history of each league is read once for all its fixtures. A database error propagates;
    the caller decides whether a missing form is worth failing for (the reviewer's is not).
    """
    by_league: defaultdict[ExternalRef, list[FixtureRecord]] = defaultdict(list)
    for record in records:
        by_league[ctx.leagues.canonical(record.fixture.league.ref)].append(record)
    forms: dict[int, tuple[TeamForm, ...]] = {}
    for league, group in by_league.items():
        start = min(r.fixture.kickoff for r in group) - FORM_LOOKBACK
        end = max(r.fixture.kickoff for r in group)
        async with ctx.sessions() as session:
            history = await finished_fixtures(
                session,
                leagues=ctx.leagues.group(league),
                start=start,
                end=end,
                preferred_sources=ctx.config.history_sources,
            )
        for record in group:
            before = [h for h in history if h.fixture.kickoff < record.fixture.kickoff]
            home = team_form(record.fixture.home, before)
            away = team_form(record.fixture.away, before)
            forms[record.id] = tuple(form for form in (home, away) if form is not None)
    return forms
