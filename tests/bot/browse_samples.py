"""Hand-made stored-data DTOs and a scriptable browser for the 🗄 Baza tests."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from app.domain import FixtureStatus, MarketType, OddsKind, Selection, Sport
from app.services.interfaces import (
    BookmakerOdds,
    DatabaseOverview,
    StoredFixture,
    StoredFixtureDetail,
    StoredLeague,
    StoredOdds,
    StoredTable,
)

NOW = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)
"""11:00 in Tashkent."""
LATE_KICKOFF = datetime(2026, 10, 21, 20, 0, tzinfo=UTC)
"""01:00 on 22.10 in Tashkent: a different local day than in UTC."""


def make_stored_fixture(fixture_id: str = "fx1", **overrides: object) -> StoredFixture:
    """A scheduled Arsenal - Chelsea fixture at :data:`LATE_KICKOFF`."""
    base = StoredFixture(
        fixture_id=fixture_id,
        sport=Sport.FOOTBALL,
        league_id="lg1",
        league="Premier League",
        country="England",
        home_team="Arsenal",
        away_team="Chelsea",
        home_team_id="tm1",
        away_team_id="tm2",
        kickoff=LATE_KICKOFF,
        status=FixtureStatus.SCHEDULED,
        home_score=None,
        away_score=None,
        has_odds=True,
        source="api-football",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def make_stored_league(league_id: str = "lg1", **overrides: object) -> StoredLeague:
    """A league with fixtures from 20.10 to 25.10."""
    base = StoredLeague(
        league_id=league_id,
        sport=Sport.FOOTBALL,
        name="Premier League",
        country="England",
        source="api-football",
        fixtures=12,
        upcoming=7,
        first_kickoff=datetime(2026, 10, 20, 15, 0, tzinfo=UTC),
        last_kickoff=datetime(2026, 10, 25, 15, 0, tzinfo=UTC),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def make_quote(
    selection: Selection = Selection.HOME,
    odds: float = 1.85,
    taken_at: datetime = NOW,
    **overrides: object,
) -> StoredOdds:
    """A 1X2 price of 1xbet."""
    base = StoredOdds(
        bookmaker="1xbet",
        market=MarketType.MATCH_RESULT,
        selection=selection,
        line=0.0,
        odds=odds,
        kind=OddsKind.MORNING,
        taken_at=taken_at,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def make_detail(**overrides: object) -> StoredFixtureDetail:
    """A fixture card with two 1X2 prices, the newer one winning."""
    older = make_quote(odds=1.80, taken_at=NOW - timedelta(hours=5))
    newer = make_quote(odds=1.85, taken_at=NOW)
    base = StoredFixtureDetail(
        fixture=make_stored_fixture(),
        season=2026,
        home_ht_score=None,
        away_ht_score=None,
        home_xg=None,
        away_xg=None,
        odds=(BookmakerOdds(bookmaker="1xbet", quotes=(older, newer)),),
        odds_total=2,
        truncated=False,
        signals=(),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def make_overview(**overrides: object) -> DatabaseOverview:
    """A healthy database refreshed three hours ago."""
    base = DatabaseOverview(
        tables=(
            StoredTable(name="leagues", rows=3, last_updated_at=NOW),
            StoredTable(name="teams", rows=40, last_updated_at=NOW),
            StoredTable(name="fixtures", rows=120, last_updated_at=NOW),
            StoredTable(name="odds_snapshots", rows=900, last_updated_at=NOW),
            StoredTable(name="signals", rows=5, own=True),
        ),
        size_bytes=13_000_000,
        last_refresh_at=NOW - timedelta(hours=3),
        earliest_kickoff=datetime(2026, 10, 20, 15, 0, tzinfo=UTC),
        latest_kickoff=datetime(2026, 10, 31, 20, 0, tzinfo=UTC),
        upcoming_fixtures=100,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


class WithOverrides:
    """A service that delegates to ``inner`` except for the named async overrides."""

    def __init__(self, inner: object, **overrides: Any) -> None:
        self._inner = inner
        self._overrides = overrides

    def __getattr__(self, name: str) -> Any:
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._inner, name)
