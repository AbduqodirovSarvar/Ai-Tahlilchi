"""Repository additions of the T4 data round (contract v1.4): catalog reads, notes, tokens."""

import asyncio
from dataclasses import replace
from datetime import time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.db.engine import create_session_factory
from app.db.enums import Confidence, DateMode, QueryScope, SignalSource, SortOrder
from app.db.repo import (
    SavedQueryDraft,
    SignalDraft,
    Thresholds,
    UserBetRecord,
    add_signal_note,
    create_bet,
    ensure_user,
    get_bet_by_token,
    get_fixtures,
    get_league,
    get_saved_query,
    get_team,
    league_refs,
    league_teams,
    list_bets,
    list_leagues,
    list_signal_notes,
    save_query,
    team_refs,
    upsert_live,
    upsert_signal,
    user_ids,
)
from app.domain import ExternalRef, FixtureStatus, Market, Selection, Sport
from tests.domain.factories import KICKOFF, make_fixture, make_league

USER, OTHER = 8001, 8002
NOW = KICKOFF - timedelta(days=1)


async def seed_signal(session: AsyncSession, user_id: int = USER) -> int:
    await ensure_user(session, user_id, timezone="UTC")
    fixture = make_fixture(status=FixtureStatus.SCHEDULED)
    await upsert_live(session, fixtures=[fixture])
    fixture_id = (await get_fixtures(session, [fixture.ref]))[fixture.ref].id
    record = await upsert_signal(
        session,
        SignalDraft(
            user_id=user_id,
            fixture_id=fixture_id,
            market=Market.match_result(),
            selection=Selection.HOME,
            odds=2.0,
            p_model=0.55,
            ev=0.1,
            confidence=Confidence.MEDIUM,
            stake_units=Decimal(1),
            source=SignalSource.DAILY,
            thresholds=Thresholds(0.45, 0.05, 1.5, 3.0),
            recommended_at=NOW,
        ),
    )
    return record.id


# --------------------------------------------------------------------------- catalog reads


async def test_catalog_reads(db_session: AsyncSession) -> None:
    epl, bundesliga = make_league(), make_league("D1", "Bundesliga")
    played = make_fixture("Arsenal", "Chelsea", league=epl)
    elsewhere = make_fixture("Bayern", "Dortmund", league=bundesliga)
    await upsert_live(db_session, fixtures=[played, elsewhere])

    leagues = await list_leagues(db_session, Sport.FOOTBALL)
    assert [league.name for _, league in leagues] == ["Bundesliga", epl.name]
    assert await list_leagues(db_session, Sport.TENNIS) == []

    league_id, league = leagues[1]
    assert await get_league(db_session, league_id) == (league_id, league)
    assert await get_league(db_session, 999_999) is None

    refs = await league_refs(db_session, [league_id, 999_999])
    assert refs == {league_id: league.ref}
    assert await league_refs(db_session, []) == {}

    teams = await league_teams(db_session, [epl.ref], source=epl.ref.source)
    assert sorted(team.name for team in teams) == ["Arsenal", "Chelsea"]
    assert await league_teams(db_session, [], source=epl.ref.source) == []
    assert await league_teams(db_session, [ExternalRef("x", "nope")], source="x") == []

    team_id = 0
    for candidate in range(1, 10):
        found = await get_team(db_session, candidate)
        if found is not None and found[1].name == "Arsenal":
            team_id = candidate
    assert team_id
    assert (await team_refs(db_session, [team_id, 999_999])).keys() == {team_id}
    assert await team_refs(db_session, []) == {}
    assert await get_team(db_session, 999_999) is None


async def test_user_ids_are_sorted(db_session: AsyncSession) -> None:
    assert await user_ids(db_session) == []
    for user in (30, 10, 20):
        await ensure_user(db_session, user, timezone="UTC")
    assert await user_ids(db_session) == [10, 20, 30]


# --------------------------------------------------------------------------- signal notes


async def test_notes_are_scoped_to_the_signal_owner(db_session: AsyncSession) -> None:
    signal_id = await seed_signal(db_session)
    await ensure_user(db_session, OTHER, timezone="UTC")

    assert await add_signal_note(db_session, OTHER, signal_id, "mine now", created_at=NOW) is None
    assert await add_signal_note(db_session, USER, 999_999, "ghost", created_at=NOW) is None

    first = await add_signal_note(db_session, USER, signal_id, "first", created_at=NOW)
    second = await add_signal_note(
        db_session, USER, signal_id, "second", created_at=NOW + timedelta(minutes=1)
    )
    again = await add_signal_note(
        db_session, USER, signal_id, "second", created_at=NOW + timedelta(minutes=1)
    )
    assert first is not None
    assert second is not None
    assert again is not None
    assert again.id != second.id  # notes are an append-only list, not upserts

    notes = await list_signal_notes(db_session, USER, signal_id)
    assert [note.text for note in notes] == ["first", "second", "second"]
    assert notes[0].user_id == USER
    assert notes[0].created_at == NOW
    assert await list_signal_notes(db_session, OTHER, signal_id) == []
    assert await list_signal_notes(db_session, USER, 999_999) == []


# --------------------------------------------------------------------------- client token


async def place(
    session: AsyncSession, signal_id: int, *, token: str | None, user: int = USER, odds: float = 2.0
) -> UserBetRecord:
    return await create_bet(
        session,
        user_id=user,
        signal_id=signal_id,
        odds_taken=odds,
        stake=Decimal(1),
        placed_at=NOW,
        client_token=token,
    )


async def test_repeating_a_token_returns_the_first_bet(db_session: AsyncSession) -> None:
    signal_id = await seed_signal(db_session)
    first = await place(db_session, signal_id, token="cb-1")
    repeat = await place(db_session, signal_id, token="cb-1", odds=9.0)
    assert repeat == first
    assert repeat.odds_taken == 2.0  # the second call wrote nothing
    assert repeat.client_token == "cb-1"
    assert len(await list_bets(db_session, USER)) == 1
    assert await get_bet_by_token(db_session, USER, "cb-1") == first
    assert await get_bet_by_token(db_session, USER, "other") is None


async def test_tokens_are_per_user_and_optional(db_session: AsyncSession) -> None:
    signal_id = await seed_signal(db_session)
    await ensure_user(db_session, OTHER, timezone="UTC")
    mine = await place(db_session, signal_id, token="same")
    theirs = await place(db_session, signal_id, token="same", user=OTHER)
    assert mine.id != theirs.id
    assert await get_bet_by_token(db_session, OTHER, "same") == theirs
    # Without a token every call is a new bet (NULLs never collide).
    first = await place(db_session, signal_id, token=None)
    second = await place(db_session, signal_id, token=None)
    assert first.id != second.id
    assert first.client_token is None


async def test_concurrent_same_token_creates_one_bet(engine: AsyncEngine) -> None:
    sessions = create_session_factory(engine)
    async with sessions.begin() as session:
        signal_id = await seed_signal(session)

    async def tap() -> int:
        async with sessions.begin() as session:
            return (await place(session, signal_id, token="race")).id

    ids = await asyncio.gather(*(tap() for _ in range(5)))
    assert len(set(ids)) == 1
    async with sessions() as session:
        assert len(await list_bets(session, USER)) == 1


async def test_a_different_integrity_error_is_not_swallowed(db_session: AsyncSession) -> None:
    await ensure_user(db_session, USER, timezone="UTC")
    with pytest.raises(IntegrityError):
        await place(db_session, 999_999, token="fk")  # unknown signal id


# --------------------------------------------------------------------------- saved queries


def query_draft(**changes: object) -> SavedQueryDraft:
    base = SavedQueryDraft(
        user_id=USER,
        name="Plan",
        date_mode=DateMode.TODAY,
        days=None,
        date_from=None,
        date_to=None,
        sports=("football",),
        min_prob=0.5,
        min_ev=0.05,
        scope=QueryScope.ALL,
        scope_ids=(),
        markets=("1x2",),
        max_signals=3,
        sort=SortOrder.EV,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


async def test_structured_schedule_and_flags_round_trip(db_session: AsyncSession) -> None:
    await ensure_user(db_session, USER, timezone="UTC")
    draft = query_draft(
        per_day=True,
        include_no_value=True,
        schedule_weekdays=(1, 3, 7),
        schedule_at=time(9, 30),
    )
    saved = await save_query(db_session, draft)
    assert saved.query == draft
    assert (await get_saved_query(db_session, USER, saved.id)) == saved

    cleared = await save_query(db_session, query_draft())
    assert cleared.id == saved.id
    assert (cleared.query.per_day, cleared.query.schedule_weekdays, cleared.query.schedule_at) == (
        False,
        (),
        None,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"schedule_weekdays": (1,)},
        {"schedule_at": time(8, 0)},
        {"schedule_weekdays": (0,), "schedule_at": time(8, 0)},
        {"schedule_weekdays": (8,), "schedule_at": time(8, 0)},
    ],
)
def test_half_a_schedule_is_refused(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="schedule_"):
        query_draft(**changes)
