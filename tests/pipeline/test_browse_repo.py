"""The read-only browse queries and the real browser service over small hand-made databases."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.enums import Confidence, SignalSource, SignalStatus
from app.db.repo import (
    FixtureFilter,
    browse_fixtures,
    browse_teams,
    database_size,
    kickoff_span,
    league_summaries,
    stored_fixture,
    stored_odds,
    table_stats,
)
from app.db.schema import FixtureRow, LeagueRow, OddsSnapshotRow, SignalRow, TeamRow, UserRow
from app.domain import FixtureStatus, MarketType, OddsKind, Selection, Sport
from app.pipeline.context import PipelineContext
from app.pipeline.freshness import mark_fresh
from app.pipeline.ledger import Ledger
from app.services.interfaces import StoredFixtureRequest, StoredTeamRequest
from app.services.real.browser import RealDataBrowserService
from app.services.real.core import Core

NOW = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)
ALICE = 11
BOB = 22


async def seed(sessions: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    """Two leagues (one empty), three teams, three fixtures, a few prices, signals of two users."""
    async with sessions.begin() as session:
        premier = LeagueRow(sport=Sport.FOOTBALL, source="s", external_id="39", name="Premier")
        empty = LeagueRow(sport=Sport.FOOTBALL, source="s", external_id="99", name="Aaa Empty")
        arsenal = TeamRow(
            sport=Sport.FOOTBALL,
            source="s",
            external_id="a",
            name="Arsenal",
            normalized_name="arsenal",
        )
        chelsea = TeamRow(
            sport=Sport.FOOTBALL,
            source="s",
            external_id="c",
            name="Chelsea",
            normalized_name="chelsea",
        )
        odd = TeamRow(
            sport=Sport.FOOTBALL,
            source="s",
            external_id="u",
            name="100%_Club",
            normalized_name="100 club",
        )
        session.add_all(
            [
                premier,
                empty,
                arsenal,
                chelsea,
                odd,
                UserRow(id=ALICE, timezone="UTC"),
                UserRow(id=BOB, timezone="UTC"),
            ]
        )
        await session.flush()
        rows = {}
        for key, kickoff, status, home, away in (
            ("past", NOW - timedelta(days=2), FixtureStatus.FINISHED, arsenal, chelsea),
            ("soon", NOW + timedelta(hours=5), FixtureStatus.SCHEDULED, chelsea, arsenal),
            ("later", NOW + timedelta(days=3), FixtureStatus.SCHEDULED, arsenal, odd),
        ):
            row = FixtureRow(
                sport=Sport.FOOTBALL,
                source="s",
                external_id=key,
                league_id=premier.id,
                season=2026,
                kickoff_at=kickoff,
                home_team_id=home.id,
                away_team_id=away.id,
                status=status,
                home_score=2 if key == "past" else None,
                away_score=1 if key == "past" else None,
            )
            session.add(row)
            rows[key] = row
        await session.flush()
        for bookmaker, taken in (("pin", NOW), ("1xbet", NOW - timedelta(hours=1))):
            session.add(
                OddsSnapshotRow(
                    fixture_id=rows["soon"].id,
                    bookmaker=bookmaker,
                    market=MarketType.MATCH_RESULT,
                    selection=Selection.HOME,
                    odds=2.0,
                    kind=OddsKind.MORNING,
                    taken_at=taken,
                )
            )
        for user, selection in (
            (ALICE, Selection.HOME),
            (ALICE, Selection.DRAW),
            (BOB, Selection.AWAY),
        ):
            session.add(
                SignalRow(
                    user_id=user,
                    fixture_id=rows["soon"].id,
                    market=MarketType.MATCH_RESULT,
                    selection=selection,
                    line=0.0,
                    odds=2.0,
                    p_model=0.5,
                    ev=0.1,
                    confidence=Confidence.MEDIUM,
                    stake_units=Decimal(1),
                    source=SignalSource.ANALYSIS,
                    status=SignalStatus.ACTIVE,
                )
            )
        await session.flush()
        return {
            "premier": premier.id,
            "empty": empty.id,
            "arsenal": arsenal.id,
            "chelsea": chelsea.id,
            **{key: row.id for key, row in rows.items()},
        }


async def test_an_empty_database_browses_to_empty_answers(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        stats = await table_stats(session, ALICE)
        span = await kickoff_span(session, NOW)
        fixtures, total = await browse_fixtures(session, FixtureFilter(), offset=0, limit=5)
        assert [s.rows for s in stats] == [0] * len(stats)
        assert (span.earliest, span.latest, span.upcoming) == (None, None, 0)
        assert (fixtures, total) == ([], 0)
        assert await league_summaries(session, NOW) == []
        assert await browse_teams(session) == ([], 0)
        assert await stored_fixture(session, 1) is None
        assert await stored_odds(session, 1) == ([], 0)
        assert await database_size(session) > 0


async def test_leagues_count_their_fixtures_and_keep_an_empty_league(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    ids = await seed(sessions)
    async with sessions() as session:
        leagues = await league_summaries(session, NOW)

    assert [league.name for league in leagues] == ["Aaa Empty", "Premier"]
    empty, premier = leagues
    assert (empty.id, empty.fixtures, empty.first_kickoff) == (ids["empty"], 0, None)
    assert premier.fixtures == 3
    assert premier.upcoming == 2
    assert premier.first_kickoff == NOW - timedelta(days=2)
    assert premier.last_kickoff == NOW + timedelta(days=3)


async def test_fixture_filters_combine_and_page(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    ids = await seed(sessions)
    async with sessions() as session:
        everything, total = await browse_fixtures(session, FixtureFilter(), offset=0, limit=2)
        rest, _ = await browse_fixtures(session, FixtureFilter(), offset=2, limit=2)
        beyond, beyond_total = await browse_fixtures(session, FixtureFilter(), offset=50, limit=2)
        upcoming, _ = await browse_fixtures(
            session, FixtureFilter(statuses=[FixtureStatus.SCHEDULED]), offset=0, limit=9
        )
        with_odds, with_total = await browse_fixtures(
            session, FixtureFilter(with_odds=True), offset=0, limit=9
        )
        without, _ = await browse_fixtures(
            session, FixtureFilter(with_odds=False), offset=0, limit=9
        )
        window, _ = await browse_fixtures(
            session, FixtureFilter(start=NOW, end=NOW + timedelta(hours=5)), offset=0, limit=9
        )
        edge, _ = await browse_fixtures(
            session,
            FixtureFilter(start=NOW, end=NOW + timedelta(hours=5, microseconds=1)),
            offset=0,
            limit=9,
        )
        team, _ = await browse_fixtures(
            session, FixtureFilter(team_ids=[ids["chelsea"]]), offset=0, limit=9
        )
        both, _ = await browse_fixtures(
            session,
            FixtureFilter(team_ids=[ids["chelsea"]], league_ids=[ids["empty"]]),
            offset=0,
            limit=9,
        )

    assert total == 3
    assert [f.id for f in everything + rest] == [ids["past"], ids["soon"], ids["later"]]
    assert (beyond, beyond_total) == ([], 3)
    assert [f.id for f in upcoming] == [ids["soon"], ids["later"]]
    assert [f.id for f in with_odds] == [ids["soon"]] and with_total == 1
    assert {f.id for f in without} == {ids["past"], ids["later"]}
    assert window == []  # the end is exclusive: a kick-off exactly at ``end`` is out
    assert [f.id for f in edge] == [ids["soon"]]
    assert [f.id for f in team] == [ids["past"], ids["soon"]]
    assert both == []  # filters are AND
    soon = next(f for f in everything + rest if f.id == ids["soon"])
    assert (soon.league, soon.home_team, soon.away_team, soon.has_odds) == (
        "Premier",
        "Chelsea",
        "Arsenal",
        True,
    )


async def test_prices_are_newest_first_and_filtered(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    ids = await seed(sessions)
    async with sessions() as session:
        rows, total = await stored_odds(session, ids["soon"])
        only, only_total = await stored_odds(session, ids["soon"], bookmaker="1xbet")
        none, none_total = await stored_odds(session, ids["soon"], market=MarketType.TOTALS)
        page, page_total = await stored_odds(session, ids["soon"], offset=1, limit=1)

    assert [r.bookmaker for r in rows] == ["pin", "1xbet"] and total == 2
    assert [r.bookmaker for r in only] == ["1xbet"] and only_total == 1
    assert (none, none_total) == ([], 0)
    assert [r.bookmaker for r in page] == ["1xbet"] and page_total == 2


async def test_team_search_ignores_case_and_treats_wildcards_literally(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    ids = await seed(sessions)
    async with sessions() as session:
        by_text, _ = await browse_teams(session, text_filter="ARSEN")
        _, wildcard_total = await browse_teams(session, text_filter="%")
        _, underscore_total = await browse_teams(session, text_filter="_")
        like_arsenal, like_total = await browse_teams(session, text_filter="ars_nal")
        by_odd_name, _ = await browse_teams(session, text_filter="100%_club")
        in_empty, empty_total = await browse_teams(session, league_ids=[ids["empty"]])
        in_premier, premier_total = await browse_teams(session, league_ids=[ids["premier"]])

    assert [t.name for t in by_text] == ["Arsenal"]
    assert by_text[0].fixtures == 3
    assert wildcard_total == 3  # punctuation normalises away: a blank filter, not a SQL wildcard
    assert underscore_total == 3
    assert (like_arsenal, like_total) == ([], 0)  # "ars_nal" is not the LIKE pattern for Arsenal
    assert [t.name for t in by_odd_name] == ["100%_Club"]
    assert (in_empty, empty_total) == ([], 0)
    assert premier_total == 3
    assert {t.name for t in in_premier} == {"Arsenal", "Chelsea", "100%_Club"}


async def test_the_table_counts_of_signals_are_the_askers_only(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await seed(sessions)
    async with sessions() as session:
        alice = {s.name: s for s in await table_stats(session, ALICE)}
        bob = {s.name: s for s in await table_stats(session, BOB)}
        nobody = {s.name: s for s in await table_stats(session, 12345)}

    assert alice["fixtures"].rows == bob["fixtures"].rows == 3
    assert (alice["signals"].rows, bob["signals"].rows, nobody["signals"].rows) == (2, 1, 0)
    assert alice["signals"].own and alice["user_bets"].own
    assert not alice["fixtures"].own
    assert alice["odds_snapshots"].last_updated_at == NOW


@pytest.fixture
def browser(sessions: async_sessionmaker[AsyncSession]) -> RealDataBrowserService:
    ctx = PipelineContext(sessions=sessions, ledger=Ledger(sessions), clock=lambda: NOW)
    return RealDataBrowserService(Core(ctx))


async def test_the_service_overview_of_an_empty_database_is_stale_and_empty(
    browser: RealDataBrowserService,
) -> None:
    overview = await browser.database_overview(ALICE)

    assert overview.last_refresh_at is None
    assert overview.data_stale
    assert overview.upcoming_fixtures == 0
    assert (overview.earliest_kickoff, overview.latest_kickoff) == (None, None)
    assert await browser.list_stored_leagues(ALICE) == ()


async def test_the_service_overview_counts_upcoming_and_marks_a_fresh_database(
    sessions: async_sessionmaker[AsyncSession], browser: RealDataBrowserService
) -> None:
    await seed(sessions)
    await mark_fresh(browser._core.ctx)

    overview = await browser.database_overview(ALICE)

    assert overview.upcoming_fixtures == 2
    assert overview.last_refresh_at == NOW
    assert not overview.data_stale


async def test_the_service_filters_by_the_users_local_day(
    sessions: async_sessionmaker[AsyncSession], browser: RealDataBrowserService
) -> None:
    await seed(sessions)
    # ALICE has no settings row: the default zone is Tashkent (UTC+5). "soon" kicks off at
    # 11:00 on 21 Oct there; "past" at 11:00 on 19 Oct; "later" at 06:00 on 24 Oct.
    page = await browser.list_stored_fixtures(
        ALICE, StoredFixtureRequest(date_from=date(2026, 10, 21), date_to=date(2026, 10, 21))
    )

    assert [f.away_team for f in page.items] == ["Arsenal"]
    teams = await browser.list_stored_teams(ALICE, StoredTeamRequest(text="chel"))
    assert [t.name for t in teams.items] == ["Chelsea"]
