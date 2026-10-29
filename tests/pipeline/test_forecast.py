"""Forecaster: model-only probabilities of provider matches from the stored history (no odds)."""

from typing import Any

import pytest

from app.domain import Market, Selection
from app.pipeline.forecast import FORECAST_MARKETS, Forecaster, ForecastSkip, chances_of
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
