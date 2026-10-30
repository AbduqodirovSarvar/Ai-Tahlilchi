"""The analysis task of the LLM: five written sections of one match; never raises."""

import json

import pytest
from pydantic import ValidationError

from app.llm.analysis import (
    MatchAnalysisInput,
    OutlookInput,
    SquadInput,
    TeamFactsInput,
    build_analysis_prompt,
)
from app.llm.budget import LlmBudget, LlmLimits
from app.llm.builder import DATA_CLOSE, DATA_OPEN
from app.llm.service import OPERATION_ANALYSIS, LlmService
from tests.llm.factories import ScriptedBackend, gemini_down


def team(name: str, *, position: int | None = None) -> TeamFactsInput:
    return TeamFactsInput(
        team=name,
        form="WWDLW",
        venue_record="home last 5: 4W 1D 0L, goals 11:3",
        goals_for_avg=2.1,
        goals_against_avg=0.8,
        over_2_5_share=0.6,
        btts_share=0.4,
        position=position,
        points=None if position is None else 20,
    )


def analysis_input(*, facts: bool = True, squads: bool = False) -> MatchAnalysisInput:
    return MatchAnalysisInput(
        league="Premier League",
        kickoff="2026-10-31 15:00 UTC",
        home_team="Arsenal",
        away_team="Chelsea",
        home=team("Arsenal", position=1) if facts else None,
        away=team("Chelsea") if facts else None,
        meetings=("2026-03-01 Arsenal 2-1 Chelsea",) if facts else (),
        outlook=OutlookInput(
            home_win=0.52,
            draw=0.26,
            away_win=0.22,
            over_2_5=0.58,
            btts=0.5,
            expected_home_goals=1.74,
            expected_away_goals=1.02,
        )
        if facts
        else None,
        home_squad=SquadInput(formation="4-3-3", coach="Mikel Arteta", starters=("David Raya",))
        if squads
        else None,
    )


def answer(**sections: str) -> str:
    body = {
        "form": "Arsenal so'nggi 5 o'yinda 3 marta yutgan.",
        "venue": "Mezbon uyda kuchli: 4 g'alaba.",
        "squads": "Tarkiblar hali e'lon qilinmagan.",
        "tactics": "Ochiq o'yin kutilmoqda.",
        "summary": "Arsenal ustunligi kutilmoqda.",
    }
    body.update(sections)
    return json.dumps(body, ensure_ascii=False)


def data_of(user: str) -> dict[str, object]:
    block = user.split(DATA_OPEN)[1].split(DATA_CLOSE, maxsplit=1)[0]
    loaded: dict[str, object] = json.loads(block)
    return loaded


def test_prompt_holds_facts_model_and_lineups_inside_the_data_block() -> None:
    built = build_analysis_prompt(analysis_input(squads=True))
    data = data_of(built.user)
    assert data["home_facts"] == {
        "form_last5": "WWDLW",
        "venue": "home last 5: 4W 1D 0L, goals 11:3",
        "avg_scored": 2.1,
        "avg_conceded": 0.8,
        "over_2_5_pct": 60,
        "btts_pct": 40,
        "table_position": 1,
        "points": 20,
    }
    assert data["model"] == {
        "home_win_pct": 52,
        "draw_pct": 26,
        "away_win_pct": 22,
        "over_2_5_pct": 58,
        "btts_pct": 50,
        "expected_goals": "1.7-1.0",
    }
    assert data["home_lineup"] == {
        "formation": "4-3-3",
        "coach": "Mikel Arteta",
        "starters": ["David Raya"],
    }
    assert data["away_lineup"] is None
    assert data["injuries"] is None  # the source has none; the prompt forbids inventing them
    assert built.prompt.version_id == "match_analysis.v1"


def test_unknown_parts_are_null_not_zero() -> None:
    data = data_of(build_analysis_prompt(analysis_input(facts=False)).user)
    assert data["home_facts"] is None
    assert data["model"] is None
    assert data["h2h"] == []


def test_inputs_are_validated() -> None:
    with pytest.raises(ValidationError):
        TeamFactsInput(
            team="A",
            form="WXL",
            venue_record="v",
            goals_for_avg=1,
            goals_against_avg=1,
            over_2_5_share=0.5,
            btts_share=0.5,
        )
    with pytest.raises(ValidationError):
        SquadInput(starters=tuple(f"P{n}" for n in range(12)))


async def test_the_sections_come_back_cleaned() -> None:
    long_tactics = "Ochiq   o'yin kutilmoqda. Mezbon hujum qiladi. Mehmon kutadi. Gol ko'p."
    backend = ScriptedBackend(answer(tactics=long_tactics))
    text = await LlmService(backend).write_analysis(analysis_input())
    assert text is not None
    assert text.form == "Arsenal so'nggi 5 o'yinda 3 marta yutgan."
    # Whitespace collapsed, at most three sentences.
    assert text.tactics == "Ochiq o'yin kutilmoqda. Mezbon hujum qiladi. Mehmon kutadi."
    assert text.count() == 5
    assert backend.requests[0]["operation"] == OPERATION_ANALYSIS
    assert backend.requests[0]["schema"] == "AnalysisResponse"


async def test_a_section_naming_someone_not_in_the_data_is_dropped_alone() -> None:
    backend = ScriptedBackend(
        answer(squads="Haaland jarohat olgan.", summary="<b>Arsenal</b> yutadi.", venue="x" * 600)
    )
    text = await LlmService(backend).write_analysis(analysis_input())
    assert text is not None
    assert (text.squads, text.summary, text.venue) == (None, None, None)
    assert text.form is not None


async def test_an_answer_with_no_usable_section_is_no_answer() -> None:
    backend = ScriptedBackend(
        answer(form="", venue="", squads="", tactics="", summary="Haaland yutadi.")
    )
    assert await LlmService(backend).write_analysis(analysis_input()) is None


@pytest.mark.parametrize(
    "backend",
    [
        ScriptedBackend(gemini_down()),
        ScriptedBackend("not json", "{}"),
        ScriptedBackend(RuntimeError("bug")),
    ],
)
async def test_failures_give_no_answer_instead_of_raising(backend: ScriptedBackend) -> None:
    assert await LlmService(backend).write_analysis(analysis_input()) is None


async def test_off_or_over_budget_gives_no_answer() -> None:
    assert await LlmService(None).write_analysis(analysis_input()) is None
    broke = LlmService(
        ScriptedBackend(answer()), budget=LlmBudget(LlmLimits(max_requests_per_day=0))
    )
    assert await broke.write_analysis(analysis_input()) is None
