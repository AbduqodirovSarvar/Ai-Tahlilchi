"""The analyst: facts, model background and line-ups for the writer; the writer's sections back."""

from dataclasses import replace
from typing import Any

from app.domain import Market, Selection
from app.llm.analysis import AnalysisText, MatchAnalysisInput
from app.pipeline.analyst import Analyst, outlook_input, squad_input, writer_input
from app.pipeline.forecast import Chance, Forecast, ForecastSkip
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
    def __init__(self, answer: AnalysisText | None = None) -> None:
        self.answer = answer
        self.seen: list[MatchAnalysisInput] = []

    async def write(self, match: MatchAnalysisInput) -> AnalysisText | None:
        self.seen.append(match)
        return self.answer


async def test_a_tracked_match_gets_facts_and_model_background(seeded_env: Env) -> None:
    writer = RecordingWriter(AnalysisText(summary="Arsenal ustunligi kutilmoqda."))
    study = await Analyst(replace(seeded_env.ctx, analysis_writer=writer)).study(
        match(1, "Arsenal", "Chelsea")
    )
    assert study.forecast.skip is None
    assert study.facts is not None
    assert study.text == writer.answer
    [sent] = writer.seen
    assert sent.home is not None
    assert sent.home.team == "Arsenal"
    assert sent.home.form == study.facts.home.form
    assert sent.outlook is not None
    total = sent.outlook.home_win + sent.outlook.draw + sent.outlook.away_win
    assert abs(total - 1.0) < 1e-6
    assert sent.outlook.expected_home_goals > 0
    assert sent.home_squad is None  # no line-up announced yet


async def test_an_untracked_match_is_still_studied_without_facts(seeded_env: Env) -> None:
    writer = RecordingWriter()
    study = await Analyst(replace(seeded_env.ctx, analysis_writer=writer)).study(
        match(2, "Arsenal", "Chelsea", code="CL")
    )
    assert study.forecast.skip is ForecastSkip.UNTRACKED_LEAGUE
    assert study.facts is None
    assert study.text is None
    [sent] = writer.seen
    assert (sent.home, sent.outlook, sent.meetings) == (None, None, ())


async def test_without_a_writer_there_is_no_text(seeded_env: Env) -> None:
    study = await Analyst(seeded_env.ctx).study(match(3, "Arsenal", "Chelsea"))
    assert study.text is None
    assert study.facts is not None


def test_line_ups_reach_the_writer_once_announced() -> None:
    announced = match(
        4,
        "Arsenal",
        "Chelsea",
        homeTeam={
            "id": 1,
            "name": "Arsenal",
            "formation": "4-3-3",
            "coach": {"name": "Mikel Arteta"},
            "lineup": [{"name": "David Raya"}, {"name": None}, {"name": "  "}],
        },
    )
    squad = squad_input(announced.home_team)
    assert squad is not None
    assert squad.starters == ("David Raya", "-")
    assert (squad.formation, squad.coach) == ("4-3-3", "Mikel Arteta")
    assert squad_input(announced.away_team) is None


def test_no_forecast_gives_no_outlook_and_long_names_are_cut() -> None:
    skipped = Forecast(match_id=5, skip=ForecastSkip.NO_MODEL)
    assert outlook_input(skipped) is None
    long_name = "A" * 120
    sent = writer_input(match(5, long_name, "Chelsea"), skipped, None)
    assert len(sent.home_team) == 80


def test_outlook_reads_the_main_markets() -> None:
    forecast = Forecast(
        match_id=6,
        chances=(
            Chance(Market.match_result(), Selection.HOME, 0.5),
            Chance(Market.match_result(), Selection.DRAW, 0.3),
            Chance(Market.match_result(), Selection.AWAY, 0.2),
            Chance(Market.totals(2.5), Selection.OVER, 0.61),
            Chance(Market.btts(), Selection.YES, 0.55),
        ),
        expected_goals=(1.6, 0.9),
    )
    outlook = outlook_input(forecast)
    assert outlook is not None
    assert (outlook.home_win, outlook.over_2_5, outlook.btts) == (0.5, 0.61, 0.55)
    assert (outlook.expected_home_goals, outlook.expected_away_goals) == (1.6, 0.9)
