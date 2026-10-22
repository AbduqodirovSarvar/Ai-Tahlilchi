"""Repository: signal upsert/versions, recommendation rules, report reads, settlement."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import (
    Confidence,
    LlmVerdict,
    Outcome,
    ResultSource,
    SignalSource,
    SignalStatus,
    SignalVersionReason,
)
from app.db.repo import (
    SignalDraft,
    SignalResultRecord,
    Thresholds,
    ensure_user,
    get_fixtures,
    get_signal,
    list_user_signals,
    report_signals,
    set_result_note,
    set_signal_status,
    settle_signal,
    signal_versions,
    signals_for_fixtures,
    unsettled_signals,
    upsert_live,
    upsert_signal,
)
from app.domain import FixtureStatus, Market, Selection
from tests.domain.factories import KICKOFF, make_fixture

USER, OTHER = 1001, 1002
NOW = KICKOFF - timedelta(days=1)
BALANCED = Thresholds(0.45, 0.05, 1.5, 3.0)
STRICT = Thresholds(0.60, 0.03, 1.3, 2.0)


async def setup(session: AsyncSession) -> tuple[int, int]:
    for user in (USER, OTHER):
        await ensure_user(session, user, timezone="Asia/Tashkent")
    early = make_fixture("Arsenal", "Chelsea", status=FixtureStatus.SCHEDULED)
    late = make_fixture(
        "Leeds", "Hull", kickoff=KICKOFF + timedelta(days=2), status=FixtureStatus.SCHEDULED
    )
    await upsert_live(session, fixtures=[early, late])
    stored = await get_fixtures(session, [early.ref, late.ref])
    return stored[early.ref].id, stored[late.ref].id


def draft(fixture_id: int, **changes: object) -> SignalDraft:
    base = SignalDraft(
        user_id=USER,
        fixture_id=fixture_id,
        market=Market.totals(2.5),
        selection=Selection.OVER,
        odds=2.0,
        p_model=0.55,
        ev=0.10,
        confidence=Confidence.MEDIUM,
        stake_units=Decimal("1.0"),
        source=SignalSource.ANALYSIS,
        thresholds=BALANCED,
        recommended_at=NOW,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


async def test_new_signal_gets_version_one(db_session: AsyncSession) -> None:
    fixture_id, _ = await setup(db_session)
    record = await upsert_signal(db_session, draft(fixture_id))
    assert record.version == 1
    assert record.market == Market.totals(2.5)
    assert record.thresholds == BALANCED
    assert record.recommended_at == NOW
    assert record.status is SignalStatus.ACTIVE
    assert record.fixture.fixture.home.name == "Arsenal"
    assert record.result is None
    assert not record.retro
    versions = await signal_versions(db_session, record.id)
    assert [(v.version, v.reason) for v in versions] == [(1, SignalVersionReason.INITIAL)]


async def test_reanalysis_updates_in_place_and_appends_versions(db_session: AsyncSession) -> None:
    fixture_id, _ = await setup(db_session)
    first = await upsert_signal(db_session, draft(fixture_id))
    later = NOW + timedelta(hours=3)
    second = await upsert_signal(
        db_session,
        draft(
            fixture_id,
            odds=1.8,
            ev=0.01,
            recommended_at=later,
            thresholds=STRICT,
            llm_verdict=LlmVerdict.CAUTION,
            llm_note="rotatsiya",
        ),
        reason=SignalVersionReason.RECALC,
    )
    assert second.id == first.id
    assert (second.version, second.odds, second.ev) == (2, 1.8, 0.01)
    assert second.recommended_at == NOW  # set once, never moved
    assert second.thresholds == BALANCED  # thresholds of the recommendation moment
    assert (second.llm_verdict, second.llm_note) == (LlmVerdict.CAUTION, "rotatsiya")
    third = await upsert_signal(db_session, draft(fixture_id), reason=SignalVersionReason.PREMATCH)
    assert third.llm_verdict is LlmVerdict.CAUTION  # not cleared by a draft without review
    versions = await signal_versions(db_session, first.id)
    assert [v.reason for v in versions] == [
        SignalVersionReason.INITIAL,
        SignalVersionReason.RECALC,
        SignalVersionReason.PREMATCH,
    ]


async def test_no_value_signal_becomes_recommended_later(db_session: AsyncSession) -> None:
    fixture_id, _ = await setup(db_session)
    shown = await upsert_signal(
        db_session, draft(fixture_id, recommended_at=None, thresholds=STRICT)
    )
    assert shown.recommended_at is None
    later = NOW + timedelta(hours=2)
    promoted = await upsert_signal(db_session, draft(fixture_id, recommended_at=later))
    assert promoted.recommended_at == later
    assert promoted.thresholds == BALANCED


async def test_retro_never_overwrites_a_real_signal(db_session: AsyncSession) -> None:
    fixture_id, _ = await setup(db_session)
    real = await upsert_signal(db_session, draft(fixture_id))
    retro = await upsert_signal(
        db_session, draft(fixture_id, source=SignalSource.RETRO, odds=5.0, recommended_at=None)
    )
    assert (retro.id, retro.odds, retro.version, retro.source) == (
        real.id,
        2.0,
        1,
        SignalSource.ANALYSIS,
    )


async def test_retro_signal_is_kept_apart_from_reports(db_session: AsyncSession) -> None:
    fixture_id, late_id = await setup(db_session)
    retro = await upsert_signal(
        db_session, draft(fixture_id, source=SignalSource.RETRO, recommended_at=None)
    )
    assert retro.retro
    upgraded = await upsert_signal(db_session, draft(fixture_id, source=SignalSource.DAILY))
    assert upgraded.source is SignalSource.DAILY
    await upsert_signal(db_session, draft(late_id, source=SignalSource.RETRO, recommended_at=NOW))
    reported = await report_signals(db_session, USER)
    assert [r.fixture.id for r in reported] == [fixture_id]


async def test_reports_include_hidden_but_not_unrecommended(db_session: AsyncSession) -> None:
    fixture_id, late_id = await setup(db_session)
    hidden = await upsert_signal(db_session, draft(fixture_id))
    await upsert_signal(db_session, draft(late_id, recommended_at=None))
    assert await set_signal_status(db_session, hidden.id, SignalStatus.HIDDEN, user_id=USER)
    reported = await report_signals(db_session, USER)
    assert [(r.id, r.status) for r in reported] == [(hidden.id, SignalStatus.HIDDEN)]
    window = await report_signals(db_session, USER, start=KICKOFF + timedelta(hours=1), end=None)
    assert window == []
    assert await report_signals(db_session, OTHER) == []


async def test_list_user_signals_hides_hidden_and_retro_by_default(
    db_session: AsyncSession,
) -> None:
    fixture_id, late_id = await setup(db_session)
    shown = await upsert_signal(db_session, draft(fixture_id))
    hidden = await upsert_signal(db_session, draft(late_id))
    await set_signal_status(db_session, hidden.id, SignalStatus.HIDDEN)
    start, end = KICKOFF - timedelta(days=1), KICKOFF + timedelta(days=5)
    default = await list_user_signals(db_session, USER, start=start, end=end)
    assert [r.id for r in default] == [shown.id]
    everything = await list_user_signals(
        db_session, USER, start=start, end=end, include_hidden=True, include_retro=True
    )
    assert [r.id for r in everything] == [shown.id, hidden.id]  # by kick-off


async def test_ownership_and_missing_signals(db_session: AsyncSession) -> None:
    fixture_id, _ = await setup(db_session)
    record = await upsert_signal(db_session, draft(fixture_id))
    assert await get_signal(db_session, record.id, user_id=OTHER) is None
    assert await get_signal(db_session, record.id, user_id=USER) == record
    assert await get_signal(db_session, 999_999) is None
    assert not await set_signal_status(db_session, record.id, SignalStatus.HIDDEN, user_id=OTHER)


async def test_signals_for_fixtures_across_users(db_session: AsyncSession) -> None:
    fixture_id, _ = await setup(db_session)
    mine = await upsert_signal(db_session, draft(fixture_id))
    theirs = await upsert_signal(db_session, draft(fixture_id, user_id=OTHER))
    cancelled = await upsert_signal(
        db_session, draft(fixture_id, market=Market.btts(), selection=Selection.YES)
    )
    await set_signal_status(db_session, cancelled.id, SignalStatus.CANCELLED)
    found = await signals_for_fixtures(db_session, [fixture_id])
    assert {r.id for r in found} == {mine.id, theirs.id}
    assert await signals_for_fixtures(db_session, []) == []


async def test_settlement_and_unsettled(db_session: AsyncSession) -> None:
    fixture_id, late_id = await setup(db_session)
    first = await upsert_signal(db_session, draft(fixture_id))
    await upsert_signal(db_session, draft(late_id))
    after_first = KICKOFF + timedelta(hours=3)
    pending = await unsettled_signals(db_session, kicked_off_before=after_first)
    assert [r.id for r in pending] == [first.id]

    settled_at = datetime(2024, 8, 17, 8, tzinfo=UTC)
    manual = SignalResultRecord(
        signal_id=first.id,
        outcome=Outcome.WIN,
        source=ResultSource.MANUAL,
        profit_units=Decimal("1.0000"),
        closing_odds=1.95,
        clv=0.025,
        user_note=None,
        settled_at=settled_at,
    )
    assert await settle_signal(db_session, manual) == manual
    api = replace(manual, outcome=Outcome.LOSS, source=ResultSource.API)
    assert (await settle_signal(db_session, api)).outcome is Outcome.WIN  # manual wins
    assert await set_result_note(db_session, first.id, "omadsizlik", user_id=USER)
    record = await get_signal(db_session, first.id)
    assert record is not None
    assert record.result is not None
    assert (record.result.outcome, record.result.user_note) == (Outcome.WIN, "omadsizlik")
    correction = replace(manual, outcome=Outcome.VOID, profit_units=Decimal(0))
    corrected = await settle_signal(db_session, correction)
    assert (corrected.outcome, corrected.user_note) == (Outcome.VOID, "omadsizlik")
    assert await unsettled_signals(db_session, kicked_off_before=after_first) == []
    assert not await set_result_note(db_session, 999_999, "x", user_id=USER)


@pytest.mark.parametrize("missing_threshold", ["min_prob", "odds_max"])
async def test_legacy_rows_without_thresholds(
    db_session: AsyncSession, missing_threshold: str
) -> None:
    fixture_id, _ = await setup(db_session)
    record = await upsert_signal(db_session, draft(fixture_id, recommended_at=None))
    from app.db.schema import SignalRow  # noqa: PLC0415 - direct row edit simulates 0002 data

    row = await db_session.get(SignalRow, record.id)
    assert row is not None
    setattr(row, missing_threshold, None)
    await db_session.flush()
    legacy = await get_signal(db_session, record.id)
    assert legacy is not None
    assert legacy.thresholds is None
    backfilled = await upsert_signal(db_session, draft(fixture_id, recommended_at=None))
    assert backfilled.thresholds == BALANCED
