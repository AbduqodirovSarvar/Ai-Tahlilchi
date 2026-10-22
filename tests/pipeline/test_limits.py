"""Loss limits (plan 6.9): money in ``Decimal``, judged per local day / ISO week, exact boundary."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.enums import Outcome
from app.db.repo import create_bet, ensure_user, settle_bet
from app.pipeline.context import PipelineContext
from app.pipeline.ledger import Ledger
from app.pipeline.limits import (
    LimitPeriod,
    LossStatus,
    limit_reached,
    load_loss_status,
    net_loss,
    week_start,
)
from app.pipeline.profile import DEFAULT_SETTINGS, UserProfile

TASHKENT = ZoneInfo("Asia/Tashkent")
USER = 41
D = Decimal


def profile_with(
    *, daily: Decimal | None = None, weekly: Decimal | None = None, zone: ZoneInfo = TASHKENT
) -> UserProfile:
    settings = replace(
        DEFAULT_SETTINGS, user_id=USER, daily_loss_limit=daily, weekly_loss_limit=weekly
    )
    return UserProfile(
        user_id=USER,
        zone=zone,
        settings=settings,
        bank=None,
        unit=None,
        watch_league_ids=frozenset(),
        watch_team_ids=frozenset(),
    )


async def settled_bet(
    sessions: async_sessionmaker[AsyncSession], profit: str, settled_at: datetime
) -> None:
    """A bet of the user, settled at ``settled_at`` with ``profit`` (placed an hour earlier)."""
    async with sessions.begin() as session:
        await ensure_user(session, USER, timezone="Asia/Tashkent")
        bet = await create_bet(
            session,
            user_id=USER,
            signal_id=None,
            odds_taken=2.0,
            stake=D(100),
            placed_at=settled_at - timedelta(hours=1),
        )
        await settle_bet(
            session,
            bet.id,
            user_id=USER,
            outcome=Outcome.LOSS if D(profit) < 0 else Outcome.WIN,
            profit=D(profit),
            settled_at=settled_at,
        )


def context(sessions: async_sessionmaker[AsyncSession], ledger: Ledger) -> PipelineContext:
    return PipelineContext(sessions=sessions, ledger=ledger)


# ----------------------------------------------------------------------------- pure rules
def test_net_loss_is_the_negated_sum_and_never_negative() -> None:
    assert net_loss([D("-30.50"), D("10.25")]) == D("20.25")
    assert net_loss([D("5"), D("-1")]) == D(0)  # a net win is no loss
    assert net_loss([]) == D(0)


def test_decimal_money_has_no_binary_noise() -> None:
    # 0.1 + 0.2 != 0.3 in binary floats; in Decimal the limit is hit exactly.
    assert net_loss([D("-0.1"), D("-0.2")]) == D("0.3")
    assert limit_reached(D("0.3"), net_loss([D("-0.1"), D("-0.2")]))


@pytest.mark.parametrize(
    ("limit", "loss", "expected"),
    [
        (D("100"), D("99.99"), False),
        (D("100"), D("100"), True),  # exactly at the limit counts
        (D("100"), D("100.0000"), True),
        (D("100"), D("100.01"), True),
        (None, D("1000000"), False),  # no limit
    ],
)
def test_the_limit_is_reached_at_exactly_the_limit(
    limit: Decimal | None, loss: Decimal, *, expected: bool
) -> None:
    assert limit_reached(limit, loss) is expected


@given(
    limit=st.decimals(min_value="0.01", max_value="100000", places=2),
    loss=st.decimals(min_value="0", max_value="100000", places=2),
)
def test_limit_reached_is_exactly_loss_at_least_limit(limit: Decimal, loss: Decimal) -> None:
    assert limit_reached(limit, loss) == (loss >= limit)


def test_the_week_starts_on_monday() -> None:
    assert week_start(date(2026, 10, 21)) == date(2026, 10, 19)  # Wednesday
    assert week_start(date(2026, 10, 19)) == date(2026, 10, 19)  # Monday itself
    assert week_start(date(2026, 10, 25)) == date(2026, 10, 19)  # Sunday


def test_the_daily_limit_wins_when_both_are_reached() -> None:
    status = LossStatus(D(100), D(300), D(100), D(250))
    assert status.period is LimitPeriod.DAY
    assert (status.limit, status.loss) == (D(100), D(100))
    weekly = LossStatus(D(10), D(300), D(100), D(250))
    assert weekly.period is LimitPeriod.WEEK
    assert (weekly.limit, weekly.loss) == (D(250), D(300))
    assert not LossStatus(D(10), D(20), D(100), D(250)).reached
    assert LossStatus(D(10), D(20), None, None).limit is None


# ----------------------------------------------------------------------------- from bets
async def test_a_user_without_limits_costs_no_query_and_is_never_limited(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    await settled_bet(sessions, "-5000", datetime(2026, 10, 21, 8, 0, tzinfo=UTC))

    status = await load_loss_status(
        context(sessions, ledger), profile_with(), datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
    )

    assert not status.reached
    assert (status.loss_today, status.loss_this_week) == (D(0), D(0))


async def test_the_daily_limit_at_the_exact_boundary(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    now = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)  # Wednesday 14:00 in Tashkent
    await settled_bet(sessions, "-60.00", datetime(2026, 10, 21, 5, 0, tzinfo=UTC))
    await settled_bet(sessions, "-39.99", datetime(2026, 10, 21, 6, 0, tzinfo=UTC))
    profile = profile_with(daily=D("100.00"))

    below = await load_loss_status(context(sessions, ledger), profile, now)
    assert below.loss_today == D("99.99")
    assert not below.reached

    await settled_bet(sessions, "-0.01", datetime(2026, 10, 21, 7, 0, tzinfo=UTC))
    at_limit = await load_loss_status(context(sessions, ledger), profile, now)
    assert at_limit.loss_today == D("100.00")
    assert at_limit.period is LimitPeriod.DAY


async def test_a_win_offsets_a_loss_of_the_same_day(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    now = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
    await settled_bet(sessions, "-150", datetime(2026, 10, 21, 5, 0, tzinfo=UTC))
    await settled_bet(sessions, "+60", datetime(2026, 10, 21, 6, 0, tzinfo=UTC))

    status = await load_loss_status(context(sessions, ledger), profile_with(daily=D(100)), now)

    assert status.loss_today == D(90)  # net, not gross
    assert not status.reached


async def test_a_bet_belongs_to_the_local_day_it_was_settled_on(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    # 19:30 UTC on Oct 21 is 00:30 on Oct 22 in Tashkent (UTC+5).
    await settled_bet(sessions, "-100", datetime(2026, 10, 21, 19, 30, tzinfo=UTC))
    profile = profile_with(daily=D(100))
    ctx = context(sessions, ledger)

    before_midnight = await load_loss_status(
        ctx, profile, datetime(2026, 10, 21, 18, 59, tzinfo=UTC)
    )
    after_midnight = await load_loss_status(
        ctx, profile, datetime(2026, 10, 21, 19, 31, tzinfo=UTC)
    )
    next_day = await load_loss_status(ctx, profile, datetime(2026, 10, 22, 19, 31, tzinfo=UTC))

    assert before_midnight.loss_today == D(0)  # still Oct 21 locally: the bet is not settled yet
    assert after_midnight.reached  # Oct 22 locally
    assert not next_day.reached  # the next local day starts clean (Oct 23)


async def test_the_weekly_limit_spans_monday_to_today_and_resets_on_monday(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    ctx = context(sessions, ledger)
    profile = profile_with(weekly=D(100))
    await settled_bet(sessions, "-60", datetime(2026, 10, 19, 6, 0, tzinfo=UTC))  # Monday
    await settled_bet(sessions, "-40", datetime(2026, 10, 21, 6, 0, tzinfo=UTC))  # Wednesday
    await settled_bet(sessions, "-500", datetime(2026, 10, 18, 6, 0, tzinfo=UTC))  # last Sunday

    midweek = await load_loss_status(ctx, profile, datetime(2026, 10, 21, 9, 0, tzinfo=UTC))
    sunday = await load_loss_status(ctx, profile, datetime(2026, 10, 25, 9, 0, tzinfo=UTC))
    next_monday = await load_loss_status(ctx, profile, datetime(2026, 10, 26, 9, 0, tzinfo=UTC))

    assert midweek.loss_this_week == D(100)
    assert midweek.period is LimitPeriod.WEEK
    assert sunday.reached  # still the same ISO week
    assert not next_monday.reached  # a new week starts clean
    assert next_monday.loss_this_week == D(0)


async def test_other_users_losses_do_not_count(
    sessions: async_sessionmaker[AsyncSession], ledger: Ledger
) -> None:
    async with sessions.begin() as session:
        await ensure_user(session, 99, timezone="Asia/Tashkent")
        other = await create_bet(
            session,
            user_id=99,
            signal_id=None,
            odds_taken=2.0,
            stake=D(100),
            placed_at=datetime(2026, 10, 21, 4, 0, tzinfo=UTC),
        )
        await settle_bet(
            session,
            other.id,
            user_id=99,
            outcome=Outcome.LOSS,
            profit=D(-900),
            settled_at=datetime(2026, 10, 21, 5, 0, tzinfo=UTC),
        )
    await settled_bet(sessions, "-10", datetime(2026, 10, 21, 5, 0, tzinfo=UTC))

    status = await load_loss_status(
        context(sessions, ledger),
        profile_with(daily=D(100)),
        datetime(2026, 10, 21, 9, 0, tzinfo=UTC),
    )

    assert status.loss_today == D(10)
