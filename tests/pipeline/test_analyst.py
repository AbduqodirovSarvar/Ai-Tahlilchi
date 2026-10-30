"""The analyst: 1-2 picks per forecastable match, by the writer (LLM) or by rule."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

import pytest

from app.domain import Market, Selection
from app.llm.picks import MatchPicksInput, PickChoice
from app.pipeline.analyst import (
    SECOND_PICK_FLOOR,
    Analyst,
    Family,
    PickOption,
    option_label,
    rule_picks,
    writer_options,
)
from app.pipeline.forecast import Chance, ForecastSkip, TeamChance, TeamPick
from app.pipeline.matches import Match
from tests.pipeline.harness import Env


def match(match_id: int, home: str, away: str, *, code: str = "PL", **extra: Any) -> Match:
    body: dict[str, Any] = {
        "id": match_id,
        "utcDate": "2026-10-25T16:00:00Z",
        "status": "TIMED",
        "competition": {"id": 2021, "name": "Premier League", "code": code},
        "homeTeam": {"id": 1000 + match_id, "name": home},
        "awayTeam": {"id": 2000 + match_id, "name": away},
    }
    body.update(extra)
    return Match.model_validate(body)


class RecordingWriter:
    def __init__(self, answer: Mapping[str, tuple[PickChoice, ...]] | None = None) -> None:
        self.answer = answer or {}
        self.seen: list[MatchPicksInput] = []

    async def write(
        self, matches: Sequence[MatchPicksInput]
    ) -> Mapping[str, tuple[PickChoice, ...]]:
        self.seen.extend(matches)
        return self.answer


def option(option_id: str, probability: float, family: Family) -> PickOption:
    chance = Chance(Market.match_result(), Selection.HOME, probability)
    return PickOption(option_id, chance, family)


async def test_without_a_writer_every_ready_match_gets_rule_picks(seeded_env: Env) -> None:
    analyses = await Analyst(seeded_env.ctx).analyse(
        [match(1, "Arsenal", "Chelsea"), match(2, "Arsenal", "Chelsea", code="CL")]
    )
    ready, untracked = analyses[1], analyses[2]
    assert 1 <= len(ready.picks) <= 2
    assert not ready.by_writer
    assert all(pick.reason is None for pick in ready.picks)
    assert ready.facts is not None
    assert ready.facts.home.form
    if len(ready.picks) == 2:
        assert ready.picks[0].option.family is not ready.picks[1].option.family
    assert untracked.forecast.skip is ForecastSkip.UNTRACKED_LEAGUE
    assert untracked.picks == ()


async def test_the_writer_chooses_among_the_model_options(seeded_env: Env) -> None:
    writer = RecordingWriter()
    ctx = replace(seeded_env.ctx, pick_writer=writer)
    await Analyst(ctx).analyse([match(3, "Arsenal", "Chelsea")])
    [sent] = writer.seen
    assert sent.match_id == "3"
    assert sent.home.team == "Arsenal"
    assert 1 <= len(sent.options) <= 8
    probabilities = [o.probability for o in sent.options]
    assert probabilities == sorted(probabilities, reverse=True)
    first = sent.options[0].option_id

    writer.answer = {
        "4": (PickChoice("made.up", "x"), PickChoice(first, "Arsenal uyda kuchli.")),
    }
    analyses = await Analyst(ctx).analyse([match(4, "Arsenal", "Chelsea")])
    [pick] = analyses[4].picks
    assert pick.option.option_id == first
    assert pick.reason == "Arsenal uyda kuchli."
    assert analyses[4].by_writer


async def test_only_unknown_choices_fall_back_to_the_rule(seeded_env: Env) -> None:
    writer = RecordingWriter({"5": (PickChoice("made.up", "x"),)})
    analyses = await Analyst(replace(seeded_env.ctx, pick_writer=writer)).analyse(
        [match(5, "Arsenal", "Chelsea")]
    )
    assert analyses[5].picks
    assert all(pick.reason is None for pick in analyses[5].picks)


def test_rule_picks_take_the_best_and_the_best_of_the_other_family() -> None:
    options = [
        option("a", 0.81, Family.RESULT),
        option("b", 0.79, Family.RESULT),
        option("c", 0.62, Family.GOALS),
    ]
    assert [p.option.option_id for p in rule_picks(options)] == ["a", "c"]
    weak = [option("a", 0.30, Family.RESULT), option("c", SECOND_PICK_FLOOR - 0.15, Family.GOALS)]
    assert [p.option.option_id for p in rule_picks(weak)] == ["a"]  # always at least one
    assert rule_picks([]) == ()


def test_writer_options_keep_both_families() -> None:
    options = [option(f"r{n}", 0.9 - n / 100, Family.RESULT) for n in range(6)]
    options.append(option("g", 0.2, Family.GOALS))
    chosen = writer_options(options)
    assert len(chosen) == 5
    assert chosen[-1].option_id == "g"


@pytest.mark.parametrize(
    ("chance", "label"),
    [
        (Chance(Market.match_result(), Selection.DRAW, 0.3), "Draw"),
        (Chance(Market.match_result(), Selection.AWAY, 0.3), "Chelsea to win"),
        (
            Chance(Market.double_chance(), Selection.HOME_OR_DRAW, 0.3),
            "Double chance 1X (Arsenal not to lose)",
        ),
        (
            Chance(Market.double_chance(), Selection.DRAW_OR_AWAY, 0.3),
            "Double chance X2 (Chelsea not to lose)",
        ),
        (Chance(Market.double_chance(), Selection.HOME_OR_AWAY, 0.3), "Double chance 12 (no draw)"),
        (Chance(Market.totals(2.5), Selection.OVER, 0.3), "Total goals over 2.5"),
        (Chance(Market.btts(), Selection.NO, 0.3), "Both teams to score: no"),
        (Chance(Market.asian_handicap(-1.0), Selection.HOME, 0.3), "Asian handicap -1: Arsenal"),
        (Chance(Market.asian_handicap(-1.0), Selection.AWAY, 0.3), "Asian handicap +1: Chelsea"),
        (TeamChance(TeamPick.AWAY_OVER_1_5, 0.3), "Chelsea over 1.5 goals"),
        (TeamChance(TeamPick.HOME_TO_SCORE, 0.3), "Arsenal to score"),
    ],
)
def test_option_labels(chance: Chance | TeamChance, label: str) -> None:
    assert option_label(PickOption("x", chance, Family.RESULT), "Arsenal", "Chelsea") == label
