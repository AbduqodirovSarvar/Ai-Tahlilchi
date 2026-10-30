"""The analyst task of the LLM: choose 1-2 given options per match and say why; never raises."""

import json

import pytest
from pydantic import ValidationError

from app.llm.budget import LlmBudget, LlmLimits
from app.llm.builder import DATA_CLOSE, DATA_OPEN
from app.llm.picks import (
    MAX_MATCHES_PER_REQUEST,
    MatchPicksInput,
    PickOptionInput,
    TeamFactsInput,
    build_picks_prompt,
)
from app.llm.service import OPERATION_PICKS, LlmService
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


def picks_input(match_id: str = "1", home: str = "Arsenal") -> MatchPicksInput:
    return MatchPicksInput(
        match_id=match_id,
        league="Premier League",
        kickoff="2026-10-31 15:00 UTC",
        home=team(home, position=1),
        away=team("Chelsea"),
        meetings=("2026-03-01 Arsenal 2-1 Chelsea",),
        options=(
            PickOptionInput(option_id="double_chance.home_draw", label="1X", probability=0.81),
            PickOptionInput(option_id="totals+2.5.over", label="Over 2.5", probability=0.58),
        ),
    )


def answer(*matches: tuple[str, list[tuple[str, str]]]) -> str:
    body = [
        {"match_id": mid, "picks": [{"option_id": oid, "reason": why} for oid, why in picks]}
        for mid, picks in matches
    ]
    return json.dumps({"matches": body}, ensure_ascii=False)


def test_prompt_holds_facts_and_options_inside_the_data_block() -> None:
    built = build_picks_prompt((picks_input(),))
    assert DATA_OPEN in built.user
    assert DATA_CLOSE in built.user
    data = json.loads(built.user.split(DATA_OPEN)[1].split(DATA_CLOSE)[0])
    [match] = data["matches"]
    assert match["home"]["table_position"] == 1
    assert "table_position" not in match["away"]
    assert match["options"][0] == {
        "option_id": "double_chance.home_draw",
        "label": "1X",
        "probability_pct": 81,
    }
    assert built.prompt.version_id == "match_picks.v1"
    assert built.included_ids == ("1",)


def test_one_request_takes_a_bounded_batch() -> None:
    with pytest.raises(ValueError, match="at most"):
        build_picks_prompt(tuple(picks_input(str(n)) for n in range(MAX_MATCHES_PER_REQUEST + 1)))


def test_inputs_are_validated() -> None:
    with pytest.raises(ValidationError):
        PickOptionInput(option_id="Bad Id!", label="x", probability=0.5)
    with pytest.raises(ValidationError):
        MatchPicksInput(
            match_id="1",
            league="L",
            kickoff="k",
            home=team("A"),
            away=team("B"),
            options=(),
        )


async def test_choices_are_kept_only_for_known_matches_and_options() -> None:
    backend = ScriptedBackend(
        answer(
            (
                "1",
                [
                    ("double_chance.home_draw", "Arsenal uyda so'nggi 5 o'yinda 4 marta yutgan."),
                    ("double_chance.home_draw", "takror"),
                    ("invented", "x"),
                    ("totals+2.5.over", "Ikkala jamoa ko'p gol uradi."),
                ],
            ),
            ("999", [("totals+2.5.over", "boshqa o'yin")]),
            ("1", [("totals+2.5.over", "ikkinchi javob e'tiborsiz")]),
        )
    )
    chosen = await LlmService(backend).write_picks([picks_input()])
    assert list(chosen) == ["1"]
    first, second = chosen["1"]
    assert first.option_id == "double_chance.home_draw"
    assert first.reason == "Arsenal uyda so'nggi 5 o'yinda 4 marta yutgan."
    assert second.option_id == "totals+2.5.over"
    assert backend.requests[0]["operation"] == OPERATION_PICKS
    assert backend.requests[0]["schema"] == "PicksResponse"


async def test_a_reason_naming_someone_not_in_the_data_is_dropped_not_the_pick() -> None:
    backend = ScriptedBackend(
        answer(("1", [("totals+2.5.over", "Haaland jarohat olgan, Liverpool ham zaif.")]))
    )
    [choice] = (await LlmService(backend).write_picks([picks_input()]))["1"]
    assert choice.option_id == "totals+2.5.over"
    assert choice.reason is None


async def test_matches_are_sent_in_batches() -> None:
    count = MAX_MATCHES_PER_REQUEST + 2
    backend = ScriptedBackend(answer(), answer(("8", [("totals+2.5.over", "Gol ko'p.")])))
    chosen = await LlmService(backend).write_picks([picks_input(str(n)) for n in range(count)])
    assert len(backend.requests) == 2
    assert list(chosen) == ["8"]


@pytest.mark.parametrize(
    "backend",
    [
        ScriptedBackend(gemini_down()),
        ScriptedBackend("not json", "{}"),
        ScriptedBackend(RuntimeError("bug")),
    ],
)
async def test_failures_give_no_choices_instead_of_raising(backend: ScriptedBackend) -> None:
    assert await LlmService(backend).write_picks([picks_input()]) == {}


async def test_off_or_over_budget_gives_no_choices() -> None:
    assert await LlmService(None).write_picks([picks_input()]) == {}
    broke = LlmService(
        ScriptedBackend(answer()), budget=LlmBudget(LlmLimits(max_requests_per_day=0))
    )
    assert await broke.write_picks([picks_input()]) == {}
    assert await LlmService(None).write_picks([]) == {}
