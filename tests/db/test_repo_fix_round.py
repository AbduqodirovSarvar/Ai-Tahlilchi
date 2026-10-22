"""T3 fix round: coherent odds kinds, rematch-safe history, user-scoped writes."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import Confidence, Outcome, ResultSource, SignalSource
from app.db.repo import (
    SignalDraft,
    SignalResultRecord,
    Thresholds,
    create_bet,
    ensure_user,
    finished_fixtures,
    get_fixtures,
    latest_odds,
    list_bets,
    set_result_note,
    settle_bet,
    settle_signal,
    upsert_live,
    upsert_signal,
)
from app.db.repo.fixtures import DUPLICATE_KICKOFF_WINDOW
from app.db.search import search_teams
from app.domain import (
    ExternalRef,
    Fixture,
    FixtureResult,
    FixtureStatus,
    Market,
    OddsKind,
    ResultStatus,
    Selection,
)
from tests.domain.factories import KICKOFF, make_fixture, make_league, make_snapshot

E0 = make_league()
DAY = timedelta(days=1)
USER, OTHER = 7001, 7002


def finished(fixture: Fixture, home: int = 1, away: int = 0) -> FixtureResult:
    return FixtureResult(
        fixture=fixture.ref, status=ResultStatus.FINISHED, home_score=home, away_score=away
    )


def source_copy(fixture: Fixture, source: str, shift: timedelta) -> Fixture:
    return replace(
        fixture, ref=ExternalRef(source, fixture.ref.external_id), kickoff=fixture.kickoff + shift
    )


# --------------------------------------------------------------------------- latest odds


async def test_one_market_never_mixes_odds_kinds(db_session: AsyncSession) -> None:
    fixture = make_fixture()
    one_x_two = {
        Selection.HOME: (OddsKind.CLOSING, 2.0),
        Selection.DRAW: (OddsKind.OPENING, 3.4),
        Selection.AWAY: (OddsKind.OPENING, 3.9),
    }
    snapshots = [
        make_snapshot(fixture, selection=selection, odds=odds, kind=kind)
        for selection, (kind, odds) in one_x_two.items()
    ]
    # The opening set is complete; closing prices exist for the home win only.
    snapshots.append(
        make_snapshot(fixture, selection=Selection.HOME, odds=2.2, kind=OddsKind.OPENING)
    )
    await upsert_live(db_session, fixtures=[fixture], odds=snapshots)
    prices = (await latest_odds(db_session, [fixture.ref]))[fixture.ref]
    assert {(s.selection, s.kind) for s in prices} == {
        (Selection.HOME, OddsKind.OPENING),
        (Selection.DRAW, OddsKind.OPENING),
        (Selection.AWAY, OddsKind.OPENING),
    }
    assert len({s.kind for s in prices}) == 1


async def test_the_preferred_kind_wins_when_it_covers_the_whole_market(
    db_session: AsyncSession,
) -> None:
    fixture = make_fixture()
    snapshots = [
        make_snapshot(fixture, selection=selection, odds=odds, kind=kind)
        for kind in (OddsKind.CLOSING, OddsKind.OPENING)
        for selection, odds in ((Selection.HOME, 2.0), (Selection.DRAW, 3.4), (Selection.AWAY, 3.9))
    ]
    await upsert_live(db_session, fixtures=[fixture], odds=snapshots)
    prices = (await latest_odds(db_session, [fixture.ref]))[fixture.ref]
    assert {s.kind for s in prices} == {OddsKind.CLOSING}
    assert len(prices) == 3


async def test_kinds_are_chosen_per_market_line_and_bookmaker(db_session: AsyncSession) -> None:
    fixture = make_fixture()
    over, under = Selection.OVER, Selection.UNDER
    snapshots = [
        # totals 2.5: closing is complete
        make_snapshot(fixture, market=Market.totals(2.5), selection=over, kind=OddsKind.CLOSING),
        make_snapshot(fixture, market=Market.totals(2.5), selection=under, kind=OddsKind.CLOSING),
        make_snapshot(fixture, market=Market.totals(2.5), selection=over, kind=OddsKind.OPENING),
        # totals 3.5: only opening exists
        make_snapshot(fixture, market=Market.totals(3.5), selection=over, kind=OddsKind.OPENING),
        make_snapshot(fixture, market=Market.totals(3.5), selection=under, kind=OddsKind.OPENING),
        # another bookmaker: only a partial closing and a partial opening
        replace(
            make_snapshot(fixture, selection=Selection.HOME, kind=OddsKind.CLOSING),
            bookmaker="1xbet",
        ),
        replace(
            make_snapshot(fixture, selection=Selection.DRAW, kind=OddsKind.OPENING),
            bookmaker="1xbet",
        ),
        replace(
            make_snapshot(fixture, selection=Selection.AWAY, kind=OddsKind.OPENING),
            bookmaker="1xbet",
        ),
    ]
    await upsert_live(db_session, fixtures=[fixture], odds=snapshots)
    prices = (await latest_odds(db_session, [fixture.ref]))[fixture.ref]
    kinds = {(s.bookmaker, str(s.market)): set() for s in prices}
    for snapshot in prices:
        kinds[(snapshot.bookmaker, str(snapshot.market))].add(snapshot.kind)
    assert kinds == {
        ("pinnacle", "totals 2.5"): {OddsKind.CLOSING},
        ("pinnacle", "totals 3.5"): {OddsKind.OPENING},
        ("1xbet", "1x2"): {OddsKind.OPENING},  # two of three selections beat one of three
    }


async def test_within_a_kind_the_latest_price_of_each_selection_wins(
    db_session: AsyncSession,
) -> None:
    fixture = make_fixture()
    first = make_snapshot(fixture, kind=OddsKind.MORNING, odds=2.0)
    second = replace(first, odds=2.2, taken_at=first.taken_at + timedelta(hours=1))
    await upsert_live(db_session, fixtures=[fixture], odds=[first, second])
    (price,) = (await latest_odds(db_session, [fixture.ref]))[fixture.ref]
    assert price.odds == 2.2


# --------------------------------------------------------------------------- history dedupe


async def test_rematch_weeks_apart_is_kept_but_a_source_twin_is_merged(
    db_session: AsyncSession,
) -> None:
    regular = make_fixture("Club Brugge", "Gent", kickoff=KICKOFF)
    playoff = make_fixture("Club Brugge", "Gent", kickoff=KICKOFF + 150 * DAY)
    regular = replace(regular, ref=ExternalRef("football-data", "r"))
    playoff = replace(playoff, ref=ExternalRef("football-data", "p"))
    live_regular = source_copy(
        replace(regular, ref=ExternalRef("x", "r")), "api-football", timedelta(hours=3)
    )
    live_playoff = source_copy(
        replace(playoff, ref=ExternalRef("x", "p")), "api-football", -timedelta(hours=2)
    )
    fixtures = [regular, playoff, live_regular, live_playoff]
    await upsert_live(db_session, fixtures=fixtures, results=[finished(f) for f in fixtures])
    rows = await finished_fixtures(
        db_session,
        leagues=[E0.ref],
        start=KICKOFF - DAY,
        end=KICKOFF + 200 * DAY,
        preferred_sources=["api-football"],
    )
    assert [r.fixture.ref for r in rows] == [live_regular.ref, live_playoff.ref]


async def test_duplicate_window_boundary(db_session: AsyncSession) -> None:
    base = make_fixture("Arsenal", "Chelsea", kickoff=KICKOFF)
    inside = source_copy(base, "api-football", DUPLICATE_KICKOFF_WINDOW)
    outside = replace(
        make_fixture(
            "Arsenal", "Chelsea", kickoff=KICKOFF + DUPLICATE_KICKOFF_WINDOW + timedelta(minutes=1)
        ),
        ref=ExternalRef("api-football", "outside"),
    )
    await upsert_live(
        db_session,
        fixtures=[base, inside, outside],
        results=[finished(f) for f in (base, inside, outside)],
    )
    rows = await finished_fixtures(
        db_session, leagues=[E0.ref], start=KICKOFF - DAY, end=KICKOFF + 10 * DAY
    )
    # `inside` (exactly at the window limit) is the twin of `base`; one minute further is not.
    assert len(rows) == 2


# --------------------------------------------------------------------------- user scope


async def test_settle_bet_and_note_are_scoped_by_user(db_session: AsyncSession) -> None:
    for user in (USER, OTHER):
        await ensure_user(db_session, user, timezone="UTC")
    fixture = make_fixture(status=FixtureStatus.SCHEDULED)
    await upsert_live(db_session, fixtures=[fixture])
    fixture_id = (await get_fixtures(db_session, [fixture.ref]))[fixture.ref].id
    signal = await upsert_signal(
        db_session,
        SignalDraft(
            user_id=USER,
            fixture_id=fixture_id,
            market=Market.match_result(),
            selection=Selection.HOME,
            odds=2.0,
            p_model=0.55,
            ev=0.1,
            confidence=Confidence.MEDIUM,
            stake_units=Decimal(1),
            source=SignalSource.ANALYSIS,
            thresholds=Thresholds(0.45, 0.05, 1.5, 3.0),
            recommended_at=KICKOFF - DAY,
        ),
    )
    await settle_signal(
        db_session,
        SignalResultRecord(
            signal_id=signal.id,
            outcome=Outcome.WIN,
            source=ResultSource.API,
            profit_units=Decimal(1),
            closing_odds=None,
            clv=None,
            user_note=None,
            settled_at=KICKOFF,
        ),
    )
    assert not await set_result_note(db_session, signal.id, "hacked", user_id=OTHER)
    assert await set_result_note(db_session, signal.id, "mine", user_id=USER)

    bet = await create_bet(
        db_session,
        user_id=USER,
        signal_id=signal.id,
        odds_taken=2.0,
        stake=Decimal(1),
        placed_at=KICKOFF - DAY,
    )
    args = {"outcome": Outcome.WIN, "profit": Decimal(1), "settled_at": KICKOFF}
    assert await settle_bet(db_session, bet.id, user_id=OTHER, **args) is None
    assert [b.outcome for b in await list_bets(db_session, USER)] == [None]
    settled = await settle_bet(db_session, bet.id, user_id=USER, **args)
    assert settled is not None
    assert settled.outcome is Outcome.WIN


# --------------------------------------------------------------------------- search


async def test_latest_league_is_the_one_of_the_most_recent_fixture(
    db_session: AsyncSession,
) -> None:
    other = make_league("D1", "Bundesliga")
    fixtures = [
        make_fixture("Wanderers", "Rovers", league=E0, kickoff=KICKOFF + n * DAY) for n in range(40)
    ]
    fixtures.append(make_fixture("Wanderers", "Bayern", league=other, kickoff=KICKOFF - DAY))
    fixtures.append(make_fixture("Rovers", "Bayern", league=other, kickoff=KICKOFF + 99 * DAY))
    await upsert_live(db_session, fixtures=fixtures)
    wanderers = {hit.name: hit.league for hit in await search_teams(db_session, "wanderers")}
    assert wanderers["Wanderers"] == E0.name  # 39 days after the Bundesliga match
    rovers = {hit.name: hit.league for hit in await search_teams(db_session, "rovers")}
    assert rovers["Rovers"] == other.name  # its latest fixture is in the other league
    assert await search_teams(db_session, "qqqzzz") == []
