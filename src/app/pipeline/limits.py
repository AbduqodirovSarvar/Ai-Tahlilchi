"""Responsible-gaming loss limits (plan section 6.9): when to stop sending new signals.

A limit is **money** (``Decimal``, in the user's bank currency), compared with the user's *net*
loss on their own bets: the sum of the profit of the bets *settled* on a local calendar day
(or in the local ISO week, Monday to today), counted as a loss when negative. The same rule
the ``/bank`` screen shows (``BankView.loss_today``), so the two never disagree. A user who
places no bets has no losses and is never limited: the limit protects money actually staked.

The limit is reached at *exactly* the limit (``loss >= limit``); a limit of ``None`` is off.
Reaching a limit stops the **new** signals of the daily digest and of scheduled templates for
the rest of that day (or week); signals already sent, their recalculations and results go on,
and a manual analysis keeps working (it carries a banner instead).
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final

from app.db.repo.operations import bets_settled_between
from app.pipeline.context import PipelineContext
from app.pipeline.profile import UserProfile, utc_bounds

_ZERO: Final = Decimal(0)


class LimitPeriod(StrEnum):
    """Which limit was reached."""

    DAY = "day"
    WEEK = "week"


def net_loss(profits: Iterable[Decimal]) -> Decimal:
    """The loss behind ``profits``: the negated sum when it is negative, otherwise zero."""
    return max(-sum(profits, _ZERO), _ZERO)


def limit_reached(limit: Decimal | None, loss: Decimal) -> bool:
    """True when ``loss`` has reached ``limit`` (exactly at the limit counts)."""
    return limit is not None and loss >= limit


def week_start(today: date) -> date:
    """The Monday of the ISO week containing ``today``."""
    return today - timedelta(days=today.weekday())


@dataclass(frozen=True, slots=True)
class LossStatus:
    """The user's losses against their limits at one moment."""

    loss_today: Decimal
    loss_this_week: Decimal
    daily_limit: Decimal | None
    weekly_limit: Decimal | None

    @property
    def period(self) -> LimitPeriod | None:
        """The reached limit (the daily one first), ``None`` when neither is reached."""
        if limit_reached(self.daily_limit, self.loss_today):
            return LimitPeriod.DAY
        if limit_reached(self.weekly_limit, self.loss_this_week):
            return LimitPeriod.WEEK
        return None

    @property
    def reached(self) -> bool:
        """True when new signals must not be sent."""
        return self.period is not None

    @property
    def limit(self) -> Decimal | None:
        """The limit of :attr:`period`."""
        if self.period is LimitPeriod.DAY:
            return self.daily_limit
        if self.period is LimitPeriod.WEEK:
            return self.weekly_limit
        return None

    @property
    def loss(self) -> Decimal:
        """The loss measured against :attr:`period`'s limit (today's when none is reached)."""
        if self.period is LimitPeriod.WEEK:
            return self.loss_this_week
        return self.loss_today


def loss_status(
    profile: UserProfile, profits_by_day: dict[date, Decimal], today: date
) -> LossStatus:
    """Losses of ``today`` and of the week so far from the daily bet profits."""
    start = week_start(today)
    return LossStatus(
        loss_today=net_loss(p for day, p in profits_by_day.items() if day == today),
        loss_this_week=net_loss(p for day, p in profits_by_day.items() if start <= day <= today),
        daily_limit=profile.settings.daily_loss_limit,
        weekly_limit=profile.settings.weekly_loss_limit,
    )


async def load_loss_status(ctx: PipelineContext, profile: UserProfile, now: datetime) -> LossStatus:
    """The user's :class:`LossStatus` at ``now`` (one query, only when a limit is set)."""
    settings = profile.settings
    today = now.astimezone(profile.zone).date()
    if settings.daily_loss_limit is None and settings.weekly_loss_limit is None:
        return LossStatus(_ZERO, _ZERO, None, None)
    start, end = utc_bounds(profile.zone, week_start(today), today)
    async with ctx.sessions() as session:
        bets = await bets_settled_between(session, profile.user_id, start=start, end=end)
    by_day: defaultdict[date, Decimal] = defaultdict(lambda: _ZERO)
    for bet in bets:
        if bet.profit is not None and bet.settled_at is not None:
            by_day[bet.settled_at.astimezone(profile.zone).date()] += bet.profit
    return loss_status(profile, by_day, today)
