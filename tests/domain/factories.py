"""Small builders for valid domain objects, shared by domain, db and sports tests."""

from datetime import UTC, datetime

from app.domain import (
    ExternalRef,
    Fixture,
    FixtureStatus,
    League,
    Market,
    OddsKind,
    OddsSnapshot,
    Selection,
    Sport,
    Team,
)

SOURCE = "test-source"
KICKOFF = datetime(2024, 8, 16, 19, 0, tzinfo=UTC)


def make_league(external_id: str = "E0", name: str = "Premier League") -> League:
    return League(
        ref=ExternalRef(SOURCE, external_id), sport=Sport.FOOTBALL, name=name, country="England"
    )


def make_team(name: str) -> Team:
    return Team(ref=ExternalRef(SOURCE, name), sport=Sport.FOOTBALL, name=name)


def make_fixture(
    home: str = "Man United",
    away: str = "Fulham",
    *,
    kickoff: datetime = KICKOFF,
    league: League | None = None,
    status: FixtureStatus = FixtureStatus.FINISHED,
) -> Fixture:
    return Fixture(
        ref=ExternalRef(SOURCE, f"{home}-{away}-{kickoff.date().isoformat()}"),
        sport=Sport.FOOTBALL,
        league=league or make_league(),
        season=2024,
        kickoff=kickoff,
        home=make_team(home),
        away=make_team(away),
        status=status,
    )


def make_snapshot(
    fixture: Fixture,
    *,
    market: Market | None = None,
    selection: Selection = Selection.HOME,
    odds: float = 2.0,
    kind: OddsKind = OddsKind.CLOSING,
) -> OddsSnapshot:
    return OddsSnapshot(
        fixture=fixture.ref,
        bookmaker="pinnacle",
        market=market or Market.match_result(),
        selection=selection,
        odds=odds,
        taken_at=fixture.kickoff,
        kind=kind,
    )
