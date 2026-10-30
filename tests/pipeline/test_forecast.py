"""Forecaster: model-only probabilities of provider matches from the stored history (no odds)."""

from typing import Any

import pytest

from app.domain import Market, Selection
from app.pipeline.forecast import (
    FORECAST_MARKETS,
    Forecaster,
    ForecastSkip,
    TeamPick,
    chances_of,
)
from app.pipeline.matches import Match
from tests.pipeline.harness import Env


def match(match_id: int, home: str, away: str, *, code: str = "PL", **extra: Any) -> Match:
    body: dict[str, Any] = {
        "id": match_id,
        "utcDate": "2026-10-25T16:00:00Z",
        "status": "TIMED",
        "area": {"id": 2072, "name": "England"},
        "competition": {"id": 2021, "name": "Premier League", "code": code},
        "homeTeam": {"id": 1000 + match_id, "name": home},
        "awayTeam": {"id": 2000 + match_id, "name": away},
    }
    body.update(extra)
    return Match.model_validate(body)


async def test_a_known_pairing_gets_every_market_and_likely_scores(seeded_env: Env) -> None:
    [forecast] = (
        await Forecaster(seeded_env.ctx).forecast([match(1, "Arsenal", "Chelsea")])
    ).values()

    assert forecast.skip is None
    result = chances_of(forecast, Market.match_result())
    assert set(result) == {Selection.HOME, Selection.DRAW, Selection.AWAY}
    assert sum(result.values()) == pytest.approx(1.0, abs=1e-6)
    assert {c.market for c in forecast.chances} == set(FORECAST_MARKETS)
    over, under = (
        chances_of(forecast, Market.totals(2.5))[s] for s in (Selection.OVER, Selection.UNDER)
    )
    assert over + under == pytest.approx(1.0, abs=1e-6)
    assert len(forecast.scores) == 3
    assert forecast.scores[0].probability >= forecast.scores[-1].probability
    assert forecast.team_matches > 0
    assert forecast.confidence is not None


async def test_provider_spellings_reach_the_history_teams(seeded_env: Env) -> None:
    forecasts = await Forecaster(seeded_env.ctx).forecast(
        [
            match(6, "Arsenal FC", "Chelsea FC"),
            match(7, "Manchester City FC", "Tottenham Hotspur FC"),
        ]
    )
    assert forecasts[6].skip is None
    assert forecasts[7].skip is None


async def test_every_match_that_cannot_be_forecast_says_why(seeded_env: Env) -> None:
    matches = [
        match(2, "Arsenal", "Chelsea", code="CL"),  # Champions League is not tracked
        match(3, "Arsenal", "Chelsea", homeTeam={"id": None, "name": None}),  # not drawn yet
        match(4, "Nowhere Rovers", "Chelsea"),  # no such team in the history
        match(5, "Ajax", "PSV", code="DED"),  # a tracked league without any stored history
    ]
    forecasts = await Forecaster(seeded_env.ctx).forecast(matches)

    assert forecasts[2].skip is ForecastSkip.UNTRACKED_LEAGUE
    assert forecasts[3].skip is ForecastSkip.TEAMS_UNDECIDED
    assert forecasts[4].skip in {ForecastSkip.UNKNOWN_TEAM, ForecastSkip.THIN_HISTORY}
    assert forecasts[5].skip is ForecastSkip.NO_MODEL
    assert all(not f.chances for f in forecasts.values())


async def test_no_matches_no_work(seeded_env: Env) -> None:
    assert await Forecaster(seeded_env.ctx).forecast([]) == {}


async def test_team_scoring_chances_agree_with_btts(seeded_env: Env) -> None:
    [forecast] = (
        await Forecaster(seeded_env.ctx).forecast([match(8, "Arsenal", "Chelsea")])
    ).values()
    by_pick = {c.pick: c.probability for c in forecast.team_chances}
    home, away = by_pick[TeamPick.HOME_TO_SCORE], by_pick[TeamPick.AWAY_TO_SCORE]
    both = chances_of(forecast, Market.btts())[Selection.YES]
    assert 0.0 < both <= min(home, away) <= max(home, away) < 1.0


async def test_handicaps_carry_their_refund_and_team_totals_nest(seeded_env: Env) -> None:
    [forecast] = (
        await Forecaster(seeded_env.ctx).forecast([match(9, "Arsenal", "Chelsea")])
    ).values()
    by_key = {(c.market, c.selection): c for c in forecast.chances}
    level = by_key[(Market.asian_handicap(0.0), Selection.HOME)]
    home_win = chances_of(forecast, Market.match_result())[Selection.HOME]
    draw = chances_of(forecast, Market.match_result())[Selection.DRAW]
    assert level.probability == pytest.approx(home_win)
    assert level.push == pytest.approx(draw)
    plus_one = by_key[(Market.asian_handicap(1.0), Selection.HOME)]
    not_lose = chances_of(forecast, Market.double_chance())[Selection.HOME_OR_DRAW]
    assert plus_one.probability == pytest.approx(not_lose)
    assert plus_one.push > 0.0
    assert by_key[(Market.btts(), Selection.YES)].push == 0.0
    team = {c.pick: c.probability for c in forecast.team_chances}
    assert 0.0 < team[TeamPick.HOME_OVER_1_5] < team[TeamPick.HOME_TO_SCORE]
    assert 0.0 < team[TeamPick.AWAY_OVER_1_5] < team[TeamPick.AWAY_TO_SCORE]
    assert forecast.fixture is not None
