"""Yellow-card forecasts: the card model of a league, fitted on the stored cards, per match.

Cards come from the history import (football-data.co.uk ``HY``/``AY``, ``Referee`` in the
English files) and from Football-Data.org bookings; the model (:mod:`app.models.cards`)
weighs each team's discipline, what it draws from opponents and the referee's strictness.

A league without enough matches with cards has no card forecast (``None``) rather than a
guess. Fits are cached per league and day: one forecast of a day's matches fits each league
once.
"""

from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Final

from app.db.repo import finished_fixtures
from app.domain import ExternalRef, Fixture
from app.logging import get_logger
from app.models import team_key
from app.models.cards import CardMatch, CardModel, fit_cards, poisson_over
from app.models.exceptions import ModelInputError
from app.pipeline.context import PipelineContext

logger = get_logger(__name__)

TOTAL_LINES: Final = (3.5, 4.5, 5.5)
TEAM_LINES: Final = (1.5, 2.5)
CACHE_SIZE: Final = 32


class CardScope(StrEnum):
    """Whose cards a line counts."""

    TOTAL = "total"
    HOME = "home"
    AWAY = "away"


@dataclass(frozen=True, slots=True)
class CardChance:
    """``P(cards > line)`` when ``over``, else ``P(cards < line)``."""

    scope: CardScope
    line: float
    over: bool
    probability: float


@dataclass(frozen=True, slots=True, kw_only=True)
class CardOutlook:
    """Expected yellow cards of one match, the referee and the lines."""

    expected_home: float
    expected_away: float
    referee: str | None
    referee_known: bool
    strictness: float
    team_matches: int
    chances: tuple[CardChance, ...]


def card_chances(expected_home: float, expected_away: float) -> tuple[CardChance, ...]:
    """Over and under of every total and team line (Poisson with the expected cards)."""
    rates = (
        (CardScope.TOTAL, expected_home + expected_away, TOTAL_LINES),
        (CardScope.HOME, expected_home, TEAM_LINES),
        (CardScope.AWAY, expected_away, TEAM_LINES),
    )
    chances: list[CardChance] = []
    for scope, rate, lines in rates:
        for line in lines:
            over = poisson_over(rate, line)
            chances.append(CardChance(scope, line, True, over))
            chances.append(CardChance(scope, line, False, 1.0 - over))
    return tuple(chances)


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time(0), tzinfo=UTC)


class CardModels:
    """Card models per league and day, fitted from the stored cards (small LRU cache)."""

    def __init__(self, ctx: PipelineContext) -> None:
        """Read history through ``ctx``."""
        self._ctx = ctx
        self._cache: OrderedDict[tuple[ExternalRef, date], CardModel | None] = OrderedDict()

    async def get(self, league: ExternalRef, as_of: date) -> CardModel | None:
        """The fitted model of ``league`` before ``as_of``, ``None`` without enough cards."""
        key = (league, as_of)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        model = await self._fit(league, as_of)
        self._cache[key] = model
        while len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)
        return model

    async def _fit(self, league: ExternalRef, as_of: date) -> CardModel | None:
        end = _day_start(as_of)
        async with self._ctx.sessions() as session:
            records = await finished_fixtures(
                session,
                leagues=self._ctx.leagues.group(league),
                start=end - timedelta(days=self._ctx.config.history_days),
                end=end,
                preferred_sources=self._ctx.config.history_sources,
            )
        matches = [
            CardMatch(
                played_on=record.fixture.kickoff.date(),
                home=team_key(record.fixture.home),
                away=team_key(record.fixture.away),
                home_yellow=record.result.home_yellow,
                away_yellow=record.result.away_yellow,
                referee=record.result.referee,
            )
            for record in records
            if record.result is not None
            and record.result.home_yellow is not None
            and record.result.away_yellow is not None
        ]
        try:
            return fit_cards(matches, as_of=as_of, xi=self._ctx.config.xi)
        except ModelInputError as error:
            logger.info("card_model_unavailable", league=str(league), reason=str(error))
            return None


async def card_outlook(
    models: CardModels, league: ExternalRef, fixture: Fixture, referee: str | None
) -> CardOutlook | None:
    """The card forecast of ``fixture`` (canonical teams), or ``None`` without a model."""
    model = await models.get(league, fixture.kickoff.date())
    if model is None:
        return None
    home, away = team_key(fixture.home), team_key(fixture.away)
    expectation = model.expected(home, away, referee)
    return CardOutlook(
        expected_home=expectation.home,
        expected_away=expectation.away,
        referee=referee,
        referee_known=expectation.referee_known,
        strictness=expectation.strictness,
        team_matches=min(model.team_matches.get(home, 0), model.team_matches.get(away, 0)),
        chances=card_chances(expectation.home, expectation.away),
    )
