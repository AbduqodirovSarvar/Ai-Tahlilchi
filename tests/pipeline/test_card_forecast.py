"""Card forecasts: the card model fitted on stored cards, per match and referee."""

from typing import Any

import pytest
from sqlalchemy import case, update

from app.db.schema import FixtureRow
from app.pipeline.card_forecast import TEAM_LINES, TOTAL_LINES, CardScope, card_chances
from app.pipeline.forecast import Forecaster
from app.pipeline.matches import Match
from tests.pipeline.harness import Env

TEAM_IDS: dict[str, int] = {}


def match(match_id: int, home: str, away: str, **extra: Any) -> Match:
    body: dict[str, Any] = {
        "id": match_id,
        "utcDate": "2026-10-25T16:00:00Z",
        "status": "TIMED",
        "competition": {"id": 2021, "name": "Premier League", "code": "PL"},
        # A club keeps one provider id across matches (as Football-Data.org sends it).
        "homeTeam": {"id": TEAM_IDS.setdefault(home, len(TEAM_IDS) + 1), "name": home},
        "awayTeam": {"id": TEAM_IDS.setdefault(away, len(TEAM_IDS) + 1), "name": away},
    }
    body.update(extra)
    return Match.model_validate(body)


async def give_cards(env: Env) -> None:
    """Every stored finished match gets cards; odd ids are refereed by a strict referee."""
    async with env.sessions.begin() as session:
        await session.execute(
            update(FixtureRow)
            .where(FixtureRow.home_score.is_not(None))
            .values(
                home_yellow=1 + FixtureRow.id % 2,
                away_yellow=2,
                referee=case((FixtureRow.id % 2 == 1, "M Strict"), else_="J Calm"),
            )
        )


def test_card_lines_are_complementary() -> None:
    chances = card_chances(1.8, 2.2)
    assert len(chances) == 2 * (len(TOTAL_LINES) + 2 * len(TEAM_LINES))
    by_key = {(c.scope, c.line, c.over): c.probability for c in chances}
    for (scope, line, over), probability in by_key.items():
        if over:
            assert probability + by_key[(scope, line, False)] == pytest.approx(1.0)
    assert by_key[(CardScope.TOTAL, 3.5, True)] > by_key[(CardScope.TOTAL, 5.5, True)]


async def test_forecasts_carry_cards_and_the_referee(seeded_env: Env) -> None:
    await give_cards(seeded_env)
    strict = match(1, "Arsenal", "Chelsea", referees=[{"name": "Mark Strict", "type": "REFEREE"}])
    calm = match(2, "Arsenal", "Chelsea", referees=[{"name": "John Calm", "type": "REFEREE"}])
    unknown = match(3, "Arsenal", "Chelsea")
    forecasts = await Forecaster(seeded_env.ctx).forecast([strict, calm, unknown])

    strict_cards, calm_cards = forecasts[1].cards, forecasts[2].cards
    assert strict_cards is not None
    assert calm_cards is not None
    assert strict_cards.referee == "Mark Strict"
    assert strict_cards.referee_known
    assert strict_cards.strictness > 1.0 > calm_cards.strictness
    assert strict_cards.expected_home + strict_cards.expected_away > (
        calm_cards.expected_home + calm_cards.expected_away
    )
    nobody = forecasts[3].cards
    assert nobody is not None
    assert nobody.referee is None
    assert not nobody.referee_known
    assert nobody.strictness == 1.0
    assert nobody.team_matches > 0


async def test_without_stored_cards_there_is_no_card_forecast(seeded_env: Env) -> None:
    forecasts = await Forecaster(seeded_env.ctx).forecast([match(4, "Arsenal", "Chelsea")])
    assert forecasts[4].skip is None
    assert forecasts[4].cards is None
