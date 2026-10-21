"""Repository: fixture windows, history for model fitting, live upserts, latest odds."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repo import (
    finished_fixtures,
    get_fixture_by_id,
    get_fixtures,
    latest_odds,
    list_fixtures,
    odds_history,
    upsert_live,
)
from app.domain import (
    ExternalRef,
    Fixture,
    FixtureResult,
    FixtureStatus,
    Market,
    OddsKind,
    ResultStatus,
    Selection,
    Sport,
)
from tests.domain.factories import KICKOFF, make_fixture, make_league, make_snapshot, make_team

E0 = make_league()
D1 = make_league("D1", "Bundesliga")
DAY = timedelta(days=1)


def finished(fixture: Fixture, home: int, away: int) -> FixtureResult:
    return FixtureResult(
        fixture=fixture.ref, status=ResultStatus.FINISHED, home_score=home, away_score=away
    )


async def seed(session: AsyncSession) -> list[Fixture]:
    fixtures = [
        make_fixture("Arsenal", "Chelsea", kickoff=KICKOFF, status=FixtureStatus.SCHEDULED),
        make_fixture("Leeds", "Hull", kickoff=KICKOFF + DAY, status=FixtureStatus.SCHEDULED),
        make_fixture(
            "Bayern", "Dortmund", kickoff=KICKOFF + DAY, league=D1, status=FixtureStatus.SCHEDULED
        ),
        make_fixture("Arsenal", "Leeds", kickoff=KICKOFF + 3 * DAY, status=FixtureStatus.SCHEDULED),
    ]
    await upsert_live(session, fixtures=fixtures)
    await session.commit()
    return fixtures


async def test_window_is_half_open_and_ordered(db_session: AsyncSession) -> None:
    fixtures = await seed(db_session)
    page = await list_fixtures(db_session, start=KICKOFF, end=KICKOFF + 3 * DAY)
    assert page.total == 3
    assert [r.fixture.ref for r in page.items] == [f.ref for f in fixtures[:3]]
    assert page.items[0].fixture.home.name == "Arsenal"
    assert page.items[0].result is None


async def test_scope_is_a_union_of_leagues_and_teams(db_session: AsyncSession) -> None:
    await seed(db_session)
    page = await list_fixtures(
        db_session,
        start=KICKOFF,
        end=KICKOFF + 10 * DAY,
        leagues=[D1.ref],
        teams=[make_team("Leeds").ref],
    )
    assert [(r.fixture.home.name, r.fixture.away.name) for r in page.items] == [
        ("Leeds", "Hull"),
        ("Bayern", "Dortmund"),
        ("Arsenal", "Leeds"),
    ]


async def test_pagination_and_filters(db_session: AsyncSession) -> None:
    await seed(db_session)
    page = await list_fixtures(db_session, start=KICKOFF, end=KICKOFF + 10 * DAY, offset=1, limit=2)
    assert (page.total, len(page.items), page.offset, page.limit) == (4, 2, 1, 2)
    empty = await list_fixtures(
        db_session,
        start=KICKOFF,
        end=KICKOFF + 10 * DAY,
        sport=Sport.HOCKEY,
        statuses=[FixtureStatus.SCHEDULED],
    )
    assert empty.total == 0
    assert empty.items == ()


async def test_get_fixtures_by_ref_and_id(db_session: AsyncSession) -> None:
    fixtures = await seed(db_session)
    found = await get_fixtures(db_session, [fixtures[0].ref, ExternalRef("x", "missing")])
    assert list(found) == [fixtures[0].ref]
    record = found[fixtures[0].ref]
    assert (await get_fixture_by_id(db_session, record.id)) == record
    assert await get_fixture_by_id(db_session, 999_999) is None
    assert await get_fixtures(db_session, []) == {}


async def test_live_results_update_status_and_scores(db_session: AsyncSession) -> None:
    fixtures = await seed(db_session)
    result = replace(finished(fixtures[0], 2, 1), home_xg=1.4, away_xg=0.9)
    summary = await upsert_live(db_session, results=[result])
    assert summary.results == 1
    record = (await get_fixtures(db_session, [fixtures[0].ref]))[fixtures[0].ref]
    assert record.fixture.status is FixtureStatus.FINISHED
    assert record.result is not None
    assert (record.result.home_score, record.home_xg) == (2, 1.4)
    again = await upsert_live(db_session, fixtures=[fixtures[0]])  # stale "scheduled" later
    assert again.fixtures == 1
    record = (await get_fixtures(db_session, [fixtures[0].ref]))[fixtures[0].ref]
    assert record.fixture.status is FixtureStatus.SCHEDULED  # merge_outcome: status wins


async def test_finished_fixtures_for_model_fitting(db_session: AsyncSession) -> None:
    fixtures = await seed(db_session)
    await upsert_live(
        db_session,
        results=[finished(fixtures[0], 1, 0), finished(fixtures[2], 3, 3)],
    )
    records = await finished_fixtures(
        db_session, leagues=[E0.ref], start=KICKOFF, end=KICKOFF + 10 * DAY
    )
    assert [r.fixture.ref for r in records] == [fixtures[0].ref]
    assert await finished_fixtures(db_session, leagues=[], start=KICKOFF, end=KICKOFF) == []
    before = await finished_fixtures(db_session, leagues=[E0.ref], start=KICKOFF - DAY, end=KICKOFF)
    assert before == []


async def test_finished_fixtures_deduplicate_sources(db_session: AsyncSession) -> None:
    history = make_fixture("Arsenal", "Chelsea")
    live = replace(
        history, ref=ExternalRef("api-football", "1"), kickoff=KICKOFF + timedelta(hours=1)
    )
    await upsert_live(
        db_session,
        fixtures=[history, live],
        results=[finished(history, 1, 0), finished(live, 1, 0)],
    )
    window = {"leagues": [E0.ref], "start": KICKOFF - DAY, "end": KICKOFF + DAY}
    preferred = await finished_fixtures(db_session, **window, preferred_sources=["api-football"])
    assert [r.fixture.ref for r in preferred] == [live.ref]
    default = await finished_fixtures(db_session, **window)
    assert [r.fixture.ref for r in default] == [history.ref]


async def test_latest_odds_by_kind_priority_then_time(db_session: AsyncSession) -> None:
    fixture = make_fixture()
    morning = make_snapshot(fixture, odds=2.0, kind=OddsKind.MORNING)
    later_morning = replace(morning, odds=2.1, taken_at=morning.taken_at + timedelta(hours=1))
    prematch = replace(morning, odds=1.9, kind=OddsKind.PREMATCH)
    other_bookmaker = replace(morning, bookmaker="1xbet", odds=2.3)
    totals = make_snapshot(
        fixture, market=Market.totals(2.5), selection=Selection.OVER, kind=OddsKind.OPENING
    )
    await upsert_live(
        db_session,
        fixtures=[fixture],
        odds=[morning, later_morning, prematch, other_bookmaker, totals],
    )
    latest = await latest_odds(db_session, [fixture.ref, ExternalRef("x", "none")])
    prices = {(s.bookmaker, str(s.market), s.selection): s.odds for s in latest[fixture.ref]}
    assert prices == {
        ("1xbet", "1x2", Selection.HOME): 2.3,
        ("pinnacle", "1x2", Selection.HOME): 1.9,
        ("pinnacle", "totals 2.5", Selection.OVER): 2.0,
    }
    mornings = await latest_odds(
        db_session, [fixture.ref], kinds=[OddsKind.MORNING], bookmakers=["pinnacle"]
    )
    assert [s.odds for s in mornings[fixture.ref]] == [2.1]
    history = await odds_history(db_session, fixture.ref, bookmakers=["pinnacle"])
    assert len(history) == 4
    assert history[-1].odds == 2.1  # oldest first: the later morning price is last
    assert await latest_odds(db_session, []) == {}
    assert await latest_odds(db_session, [fixture.ref], kinds=[]) == {}


async def test_latest_odds_for_fixture_without_prices(db_session: AsyncSession) -> None:
    fixture = make_fixture()
    await upsert_live(db_session, fixtures=[fixture])
    assert await latest_odds(db_session, [fixture.ref]) == {fixture.ref: []}


async def test_naive_and_aware_windows_compare_in_utc(db_session: AsyncSession) -> None:
    await seed(db_session)
    tashkent = KICKOFF.astimezone(datetime(2024, 1, 1, tzinfo=UTC).astimezone().tzinfo)
    page = await list_fixtures(db_session, start=tashkent, end=tashkent + DAY)
    assert page.total == 1
