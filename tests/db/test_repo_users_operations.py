"""Repository: users, settings, watchlist, saved queries, recalc queue, bets, API usage."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.clients.quota import (
    InMemoryUsageStore,
    QuotaGuard,
    QuotaPolicy,
    RequestPurpose,
    UsageCounts,
    UsageKey,
)
from app.db.api_usage import ApiUsageRecord, SqlUsageStore, usage_on
from app.db.engine import create_session_factory
from app.db.enums import (
    Confidence,
    DateMode,
    Outcome,
    QueryScope,
    RecalcKind,
    RiskProfile,
    SignalSource,
    SortOrder,
    WatchKind,
)
from app.db.repo import (
    SavedQueryDraft,
    SignalDraft,
    Thresholds,
    UserSettingsRecord,
    add_watch,
    bets_for_signal,
    bets_placed_between,
    cancel_recalcs,
    claim_due_recalcs,
    complete_recalc,
    create_bet,
    delete_saved_query,
    enqueue_recalc,
    ensure_user,
    get_fixtures,
    get_saved_query,
    get_user,
    get_user_settings,
    list_bets,
    list_saved_queries,
    list_watchlist,
    release_recalc,
    remove_watch,
    save_query,
    set_watch_notifications,
    settle_bet,
    update_user,
    upsert_live,
    upsert_signal,
    upsert_user_settings,
)
from app.domain import FixtureStatus, Market, Selection, Sport
from tests.domain.factories import KICKOFF, make_fixture

USER = 7
NOW = datetime(2024, 8, 16, 9, 0, tzinfo=UTC)


async def test_users_are_created_once_and_updated(db_session: AsyncSession) -> None:
    created = await ensure_user(db_session, USER, timezone="Asia/Tashkent", is_admin=True)
    again = await ensure_user(db_session, USER, timezone="Europe/London")
    assert again == created
    assert again.timezone == "Asia/Tashkent"
    updated = await update_user(
        db_session, USER, timezone="UTC", bank=Decimal("100.5"), unit=Decimal(1)
    )
    assert updated is not None
    assert (updated.timezone, updated.bank) == ("UTC", Decimal("100.5000"))
    assert await get_user(db_session, USER) == updated
    assert await get_user(db_session, 999) is None
    assert await update_user(db_session, 999, timezone="UTC", bank=None, unit=None) is None


def settings_record(**changes: object) -> UserSettingsRecord:
    record = UserSettingsRecord(
        user_id=USER,
        sports=("football",),
        leagues=(1, 2),
        markets=("1x2", "totals"),
        max_signals=10,
        min_prob=0.45,
        min_ev=0.05,
        odds_min=1.5,
        odds_max=3.0,
        risk_profile=RiskProfile.BALANCED,
        send_time=time(9, 0),
        prematch_minutes=60,
        quiet_hours_start=time(0, 0),
        quiet_hours_end=time(8, 0),
        daily_loss_limit=Decimal("5.0000"),
    )
    return replace(record, **changes)  # type: ignore[arg-type]


async def test_settings_round_trip_and_replace(db_session: AsyncSession) -> None:
    await ensure_user(db_session, USER, timezone="Asia/Tashkent")
    assert await get_user_settings(db_session, USER) is None
    stored = await upsert_user_settings(db_session, settings_record())
    assert stored == settings_record()
    changed = settings_record(
        leagues=(), risk_profile=RiskProfile.CUSTOM, daily_loss_limit=None, prematch_minutes=None
    )
    assert await upsert_user_settings(db_session, changed) == changed
    assert await get_user_settings(db_session, USER) == changed


async def test_watchlist_is_idempotent_and_owned(db_session: AsyncSession) -> None:
    await ensure_user(db_session, USER, timezone="UTC")
    await ensure_user(db_session, USER + 1, timezone="UTC")
    first = await add_watch(
        db_session, USER, kind=WatchKind.TEAM, sport=Sport.FOOTBALL, entity_id=5,
        notify_flags=("lineups",),
    )  # fmt: skip
    again = await add_watch(
        db_session, USER, kind=WatchKind.TEAM, sport=Sport.FOOTBALL, entity_id=5
    )
    assert again == first
    await add_watch(db_session, USER, kind=WatchKind.LEAGUE, sport=Sport.FOOTBALL, entity_id=5)
    assert [w.kind for w in await list_watchlist(db_session, USER)] == [
        WatchKind.TEAM,
        WatchKind.LEAGUE,
    ]
    flags = await set_watch_notifications(
        db_session, USER, kind=WatchKind.TEAM, entity_id=5, notify_flags=("result", "odds_move")
    )
    assert flags is not None
    assert flags.notify_flags == ("result", "odds_move")
    assert (
        await set_watch_notifications(
            db_session, USER + 1, kind=WatchKind.TEAM, entity_id=5, notify_flags=()
        )
        is None
    )
    assert not await remove_watch(db_session, USER + 1, kind=WatchKind.TEAM, entity_id=5)
    assert await remove_watch(db_session, USER, kind=WatchKind.TEAM, entity_id=5)
    assert len(await list_watchlist(db_session, USER)) == 1


def query_draft(**changes: object) -> SavedQueryDraft:
    draft = SavedQueryDraft(
        user_id=USER,
        name="Dam olish",
        date_mode=DateMode.WEEKEND,
        days=None,
        date_from=None,
        date_to=None,
        sports=("football",),
        min_prob=0.5,
        min_ev=0.05,
        scope=QueryScope.LEAGUES,
        scope_ids=(1, 2),
        markets=("1x2",),
        max_signals=5,
        sort=SortOrder.EV,
    )
    return replace(draft, **changes)  # type: ignore[arg-type]


async def test_saved_queries_crud(db_session: AsyncSession) -> None:
    await ensure_user(db_session, USER, timezone="UTC")
    saved = await save_query(db_session, query_draft())
    assert saved.query == query_draft()
    replaced = await save_query(
        db_session,
        query_draft(date_mode=DateMode.RANGE, date_from=date(2024, 8, 1), date_to=date(2024, 8, 3)),
    )
    assert replaced.id == saved.id
    other = await save_query(db_session, query_draft(name="Bugun", date_mode=DateMode.TODAY))
    assert [q.query.name for q in await list_saved_queries(db_session, USER)] == [
        "Bugun",
        "Dam olish",
    ]
    assert await get_saved_query(db_session, USER, other.id) == other
    assert await get_saved_query(db_session, USER + 1, other.id) is None
    assert not await delete_saved_query(db_session, USER + 1, other.id)
    assert await delete_saved_query(db_session, USER, other.id)
    assert await get_saved_query(db_session, USER, other.id) is None


async def make_signal(session: AsyncSession) -> int:
    await ensure_user(session, USER, timezone="UTC")
    fixture = make_fixture(status=FixtureStatus.SCHEDULED)
    await upsert_live(session, fixtures=[fixture])
    fixture_id = (await get_fixtures(session, [fixture.ref]))[fixture.ref].id
    record = await upsert_signal(
        session,
        SignalDraft(
            user_id=USER,
            fixture_id=fixture_id,
            market=Market.match_result(),
            selection=Selection.HOME,
            odds=2.0,
            p_model=0.55,
            ev=0.1,
            confidence=Confidence.HIGH,
            stake_units=Decimal(1),
            source=SignalSource.DAILY,
            thresholds=Thresholds(0.45, 0.05, 1.5, 3.0),
            recommended_at=NOW,
        ),
    )
    return record.id


async def test_recalc_queue_claims_each_task_once(db_session: AsyncSession) -> None:
    signal_id = await make_signal(db_session)
    pre = await enqueue_recalc(
        db_session, signal_id, kind=RecalcKind.PREMATCH, run_at=KICKOFF - timedelta(hours=1)
    )
    day = await enqueue_recalc(
        db_session, signal_id, kind=RecalcKind.BEFORE_24H, run_at=KICKOFF - timedelta(days=1)
    )
    assert await claim_due_recalcs(db_session, now=KICKOFF - timedelta(days=2)) == []
    claimed = await claim_due_recalcs(db_session, now=KICKOFF - timedelta(minutes=30))
    assert [t.id for t in claimed] == [day.id, pre.id]  # earliest first
    assert all(t.claimed_at is not None and not t.done for t in claimed)
    assert await claim_due_recalcs(db_session, now=KICKOFF - timedelta(minutes=29)) == []
    owned = {task.id: task.claimed_at for task in claimed}
    assert owned[day.id] is not None
    assert owned[pre.id] is not None
    assert await complete_recalc(db_session, day.id, now=KICKOFF, claimed_at=owned[day.id])
    assert not await complete_recalc(db_session, day.id, now=KICKOFF, claimed_at=owned[day.id])
    assert await release_recalc(db_session, pre.id, claimed_at=owned[pre.id])
    reclaimed = await claim_due_recalcs(db_session, now=KICKOFF, limit=1)
    assert [t.id for t in reclaimed] == [pre.id]


async def test_expired_claim_can_be_taken_again(db_session: AsyncSession) -> None:
    signal_id = await make_signal(db_session)
    task = await enqueue_recalc(db_session, signal_id, kind=RecalcKind.PREMATCH, run_at=NOW)
    assert len(await claim_due_recalcs(db_session, now=NOW)) == 1
    later = NOW + timedelta(minutes=16)  # default lease is 15 minutes
    assert [t.id for t in await claim_due_recalcs(db_session, now=later)] == [task.id]


async def test_enqueue_is_idempotent_and_reopens_on_new_time(db_session: AsyncSession) -> None:
    signal_id = await make_signal(db_session)
    task = await enqueue_recalc(db_session, signal_id, kind=RecalcKind.PREMATCH, run_at=NOW)
    (claimed,) = await claim_due_recalcs(db_session, now=NOW)
    assert claimed.claimed_at is not None
    await complete_recalc(db_session, task.id, now=NOW, claimed_at=claimed.claimed_at)
    same = await enqueue_recalc(db_session, signal_id, kind=RecalcKind.PREMATCH, run_at=NOW)
    assert (same.id, same.done) == (task.id, True)
    moved = NOW + timedelta(hours=2)
    reopened = await enqueue_recalc(db_session, signal_id, kind=RecalcKind.PREMATCH, run_at=moved)
    assert (reopened.id, reopened.done, reopened.run_at, reopened.claimed_at) == (
        task.id,
        False,
        moved,
        None,
    )
    assert await cancel_recalcs(db_session, signal_id, now=NOW) == 1
    assert await claim_due_recalcs(db_session, now=moved) == []


async def test_bets(db_session: AsyncSession) -> None:
    signal_id = await make_signal(db_session)
    first = await create_bet(
        db_session,
        user_id=USER,
        signal_id=signal_id,
        odds_taken=2.05,
        stake=Decimal("2.5"),
        placed_at=NOW,
    )
    second = await create_bet(
        db_session,
        user_id=USER,
        signal_id=None,
        odds_taken=1.8,
        stake=Decimal(1),
        placed_at=NOW + timedelta(hours=1),
    )
    assert [b.id for b in await list_bets(db_session, USER)] == [second.id, first.id]
    settled = await settle_bet(
        db_session,
        first.id,
        user_id=USER,
        outcome=Outcome.WIN,
        profit=Decimal("2.625"),
        settled_at=NOW,
    )
    assert settled is not None
    assert settled.profit == Decimal("2.6250")
    assert [b.id for b in await list_bets(db_session, USER, open_only=True)] == [second.id]
    assert [b.id for b in await bets_for_signal(db_session, signal_id)] == [first.id]
    window = await bets_placed_between(db_session, USER, start=NOW, end=NOW + timedelta(minutes=30))
    assert [b.id for b in window] == [first.id]
    assert (
        await settle_bet(
            db_session, 999, user_id=USER, outcome=Outcome.LOSS, profit=Decimal(-1), settled_at=NOW
        )
        is None
    )


async def test_sql_usage_store_counts_and_reconciles(engine: AsyncEngine) -> None:
    store = SqlUsageStore(create_session_factory(engine))
    key = UsageKey(date(2024, 10, 26), "api-football", "football")
    assert await store.get(key) == UsageCounts()
    assert await store.add(key, requests=2, manual=0) == UsageCounts(2, 0)
    assert await store.add(key, requests=1, manual=1) == UsageCounts(3, 1)
    assert await store.raise_floor(key, 10) == UsageCounts(10, 1)
    assert await store.raise_floor(key, 4) == UsageCounts(10, 1)
    fresh = UsageKey(date(2024, 10, 27), "api-football", "football")
    assert await store.raise_floor(fresh, 5) == UsageCounts(5, 0)
    async with create_session_factory(engine)() as session:
        assert await usage_on(session, date(2024, 10, 26)) == [
            ApiUsageRecord(key, UsageCounts(10, 1))
        ]


async def test_sql_store_backs_the_quota_guard(engine: AsyncEngine) -> None:
    sql_quota = QuotaGuard(
        SqlUsageStore(create_session_factory(engine)),
        QuotaPolicy(daily_limit=10),
        provider="api-football",
        clock=lambda: NOW,
    )
    memory_quota = QuotaGuard(
        InMemoryUsageStore(),
        QuotaPolicy(daily_limit=10),
        provider="api-football",
        clock=lambda: NOW,
    )
    for quota in (sql_quota, memory_quota):
        await quota.charge(RequestPurpose.AUTOMATIC, 7)
        status = await quota.charge(RequestPurpose.MANUAL, 2)
        assert (status.remaining, status.automatic_available) == (1, 0)
