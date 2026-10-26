"""Constraint and relationship behaviour of app.db.schema on a real SQLite file."""

from datetime import UTC, datetime, time
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError, InvalidRequestError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import (
    Confidence,
    RiskProfile,
    SignalSource,
    SignalStatus,
)
from app.db.schema import (
    Base,
    FixtureRow,
    LeagueRow,
    OddsSnapshotRow,
    SignalRow,
    TeamRow,
    UserRow,
    UserSettingsRow,
)
from app.domain import FixtureStatus, MarketType, OddsKind, Selection, Sport

PLAN_TABLES = {
    "provider_calls",
    "learning_changes",
    "pipeline_ledger",
    "signal_notes",
    "fixtures",
    "odds_snapshots",
    "signals",
    "signal_versions",
    "results",
    "postmortems",
    "lessons",
    "segment_stats",
    "calibration_versions",
    "users",
    "user_settings",
    "watchlist",
    "user_bets",
    "saved_queries",
    "scheduled_recalcs",
    "api_usage",
    "leagues",
    "teams",
}
KICKOFF = datetime(2024, 8, 16, 19, tzinfo=UTC)


def test_schema_has_every_table_of_the_plan() -> None:
    assert set(Base.metadata.tables) == PLAN_TABLES


async def add_fixture(session: AsyncSession, external_id: str = "f1") -> FixtureRow:
    league = LeagueRow(sport=Sport.FOOTBALL, source="src", external_id="E0", name="EPL")
    home = TeamRow(
        sport=Sport.FOOTBALL, source="src", external_id="a", name="A", normalized_name="a"
    )
    away = TeamRow(
        sport=Sport.FOOTBALL, source="src", external_id="b", name="B", normalized_name="b"
    )
    session.add_all([league, home, away])
    await session.flush()
    fixture = FixtureRow(
        sport=Sport.FOOTBALL,
        source="src",
        external_id=external_id,
        league_id=league.id,
        season=2024,
        kickoff_at=KICKOFF,
        home_team_id=home.id,
        away_team_id=away.id,
        status=FixtureStatus.SCHEDULED,
    )
    session.add(fixture)
    await session.flush()
    return fixture


def odds_row(fixture_id: int, **overrides: object) -> OddsSnapshotRow:
    values: dict[str, object] = {
        "fixture_id": fixture_id,
        "bookmaker": "pinnacle",
        "market": MarketType.MATCH_RESULT,
        "selection": Selection.HOME,
        "odds": 2.0,
        "kind": OddsKind.CLOSING,
        "taken_at": KICKOFF,
    }
    values.update(overrides)
    return OddsSnapshotRow(**values)


async def test_fixture_external_identity_is_unique(db_session: AsyncSession) -> None:
    fixture = await add_fixture(db_session)
    db_session.add(
        FixtureRow(
            sport=Sport.FOOTBALL,
            source="src",
            external_id="f1",
            league_id=fixture.league_id,
            season=2024,
            kickoff_at=KICKOFF,
            home_team_id=fixture.home_team_id,
            away_team_id=fixture.away_team_id,
            status=FixtureStatus.SCHEDULED,
        )
    )
    with pytest.raises(IntegrityError, match="UNIQUE"):
        await db_session.flush()


async def test_team_cannot_play_itself(db_session: AsyncSession) -> None:
    fixture = await add_fixture(db_session)
    fixture.away_team_id = fixture.home_team_id
    with pytest.raises(IntegrityError, match="distinct_teams"):
        await db_session.flush()


async def test_foreign_keys_are_enforced(db_session: AsyncSession) -> None:
    db_session.add(odds_row(fixture_id=999))
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        await db_session.flush()


@pytest.mark.parametrize("odds", [1.0, 0.5])
async def test_odds_must_be_above_one(db_session: AsyncSession, odds: float) -> None:
    fixture = await add_fixture(db_session)
    db_session.add(odds_row(fixture.id, odds=odds))
    with pytest.raises(IntegrityError, match="odds_above_one"):
        await db_session.flush()


async def test_line_is_only_allowed_on_line_markets(db_session: AsyncSession) -> None:
    fixture = await add_fixture(db_session)
    db_session.add(
        odds_row(fixture.id, market=MarketType.TOTALS, selection=Selection.OVER, line=2.5)
    )
    await db_session.flush()
    db_session.add(odds_row(fixture.id, line=2.5))
    with pytest.raises(IntegrityError, match="line_only_for_line_markets"):
        await db_session.flush()


async def test_same_price_identity_cannot_be_stored_twice(db_session: AsyncSession) -> None:
    fixture = await add_fixture(db_session)
    db_session.add_all([odds_row(fixture.id), odds_row(fixture.id, odds=2.1)])
    with pytest.raises(IntegrityError, match="UNIQUE"):
        await db_session.flush()


async def test_unknown_enum_value_is_rejected_before_reaching_the_database(
    db_session: AsyncSession,
) -> None:
    fixture = await add_fixture(db_session)
    db_session.add(odds_row(fixture.id, kind="sometime"))
    with pytest.raises(StatementError, match="sometime"):
        await db_session.flush()


async def test_deleting_a_fixture_cascades_to_its_odds(db_session: AsyncSession) -> None:
    fixture = await add_fixture(db_session)
    db_session.add(odds_row(fixture.id))
    await db_session.flush()
    await db_session.execute(delete(FixtureRow).where(FixtureRow.id == fixture.id))
    remaining = (await db_session.execute(select(OddsSnapshotRow))).scalars().all()
    assert remaining == []


async def test_leagues_in_use_cannot_be_deleted(db_session: AsyncSession) -> None:
    fixture = await add_fixture(db_session)
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        await db_session.execute(delete(LeagueRow).where(LeagueRow.id == fixture.league_id))


async def test_relationships_refuse_implicit_lazy_loading(db_session: AsyncSession) -> None:
    await add_fixture(db_session)
    await db_session.commit()
    db_session.expunge_all()
    fixture = (await db_session.execute(select(FixtureRow))).scalar_one()
    with pytest.raises(InvalidRequestError, match="lazy='raise'"):
        _ = fixture.league


def user_settings(**overrides: object) -> UserSettingsRow:
    values: dict[str, object] = {
        "user_id": 7,
        "sports": ["football"],
        "leagues": [1, 2],
        "markets": ["1x2", "totals"],
        "max_signals": 10,
        "min_prob": 0.5,
        "min_ev": 0.05,
        "odds_min": 1.4,
        "odds_max": 3.0,
        "risk_profile": RiskProfile.BALANCED,
        "send_time": time(9, 0),
        "prematch_minutes": 60,
    }
    values.update(overrides)
    return UserSettingsRow(**values)


async def test_user_settings_round_trip_and_cascade_with_user(db_session: AsyncSession) -> None:
    db_session.add(UserRow(id=7, timezone="Asia/Tashkent", bank=Decimal(100)))
    await db_session.flush()
    db_session.add(user_settings())
    await db_session.commit()
    db_session.expunge_all()

    stored = (await db_session.execute(select(UserSettingsRow))).scalar_one()
    assert (stored.leagues, stored.markets, stored.send_time) == (
        [1, 2],
        ["1x2", "totals"],
        time(9),
    )
    await db_session.execute(delete(UserRow).where(UserRow.id == 7))
    assert (await db_session.execute(select(UserSettingsRow))).first() is None


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"min_prob": 1.5}, "min_prob_range"),
        ({"odds_min": 2.0, "odds_max": 1.5}, "odds_range"),
        ({"max_signals": 0}, "max_signals_positive"),
    ],
)
async def test_user_settings_ranges_are_checked(
    db_session: AsyncSession, overrides: dict[str, object], constraint: str
) -> None:
    db_session.add(UserRow(id=7, timezone="UTC"))
    await db_session.flush()
    db_session.add(user_settings(**overrides))
    with pytest.raises(IntegrityError, match=constraint):
        await db_session.flush()


async def test_one_signal_per_user_and_selection(db_session: AsyncSession) -> None:
    fixture = await add_fixture(db_session)
    db_session.add(UserRow(id=7, timezone="UTC"))
    await db_session.flush()

    def signal() -> SignalRow:
        return SignalRow(
            user_id=7,
            fixture_id=fixture.id,
            market=MarketType.MATCH_RESULT,
            selection=Selection.HOME,
            odds=2.0,
            p_model=0.55,
            ev=0.1,
            confidence=Confidence.HIGH,
            stake_units=Decimal(1),
            source=SignalSource.DAILY,
            status=SignalStatus.ACTIVE,
        )

    db_session.add(signal())
    await db_session.flush()
    db_session.add(signal())
    with pytest.raises(IntegrityError, match="UNIQUE"):
        await db_session.flush()
