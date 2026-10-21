"""Small builders of domain objects for model tests."""

from datetime import UTC, datetime, timedelta
from typing import Any

from app.domain import (
    BOOKMAKER_PINNACLE,
    ExternalRef,
    Fixture,
    FixtureResult,
    League,
    Market,
    OddsKind,
    OddsSnapshot,
    ResultStatus,
    Selection,
    Sport,
    Team,
)

LEAGUE = League(ref=ExternalRef("test", "L1"), sport=Sport.FOOTBALL, name="Test League")
KICKOFF = datetime(2025, 3, 1, 15, 0, tzinfo=UTC)


def team(name: str) -> Team:
    return Team(ref=ExternalRef("test", name), sport=Sport.FOOTBALL, name=name)


def fixture(
    home: str = "Home", away: str = "Away", kickoff: datetime = KICKOFF, ident: str | None = None
) -> Fixture:
    return Fixture(
        ref=ExternalRef("test", ident or f"{home}-{away}-{kickoff:%Y%m%d%H%M}"),
        sport=Sport.FOOTBALL,
        league=LEAGUE,
        season=kickoff.year if kickoff.month >= 7 else kickoff.year - 1,
        kickoff=kickoff,
        home=team(home),
        away=team(away),
    )


def finished(match: Fixture, home_goals: int, away_goals: int) -> FixtureResult:
    return FixtureResult(
        fixture=match.ref,
        status=ResultStatus.FINISHED,
        home_score=home_goals,
        away_score=away_goals,
    )


def snapshots(
    match: Fixture, market: Market, prices: dict[Selection, float], **overrides: Any
) -> list[OddsSnapshot]:
    """Pinnacle opening snapshots a day before kickoff; ``overrides`` replace any field."""
    defaults: dict[str, Any] = {
        "bookmaker": BOOKMAKER_PINNACLE,
        "kind": OddsKind.OPENING,
        "taken_at": match.kickoff - timedelta(days=1),
    }
    fields = {**defaults, **overrides}
    return [
        OddsSnapshot(fixture=match.ref, market=market, selection=selection, odds=price, **fields)
        for selection, price in prices.items()
    ]
