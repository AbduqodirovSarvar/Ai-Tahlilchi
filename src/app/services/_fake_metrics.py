"""Report arithmetic of the fake services (private to ``fake``).

The real numbers will come from the pipeline and models; these simple versions only have to
be self-consistent so the bot's report screens can be built and tested against them.
"""

import itertools
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from app.services.interfaces import (
    CalibrationBucket,
    DaySummary,
    ReportGroup,
    ReportView,
    ResultMark,
    RiskProfile,
    SegmentStat,
    SignalView,
)

_WINS: Final = frozenset({ResultMark.WON, ResultMark.HALF_WON})
_LOSSES: Final = frozenset({ResultMark.LOST, ResultMark.HALF_LOST})
_CALIBRATION_EDGES: Final = (0.0, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0)
_HALF: Final = Decimal("0.5")


@dataclass(frozen=True, slots=True, kw_only=True)
class SignalOutcome:
    """A signal with what the report needs beyond its card."""

    view: SignalView
    profit_units: Decimal | None
    clv: float | None
    risk_profile: RiskProfile


def profit_units(view: SignalView, cashout_units: Decimal | None) -> Decimal | None:
    """Profit of the recommended stake, ``None`` while unsettled."""
    if view.result is None:
        return None
    if view.result is ResultMark.CASHOUT:
        return cashout_units or Decimal(0)
    stake = view.stake_units
    win = stake * (Decimal(str(view.odds)) - 1)
    by_mark = {
        ResultMark.WON: win,
        ResultMark.HALF_WON: win * _HALF,
        ResultMark.LOST: -stake,
        ResultMark.HALF_LOST: -stake * _HALF,
        ResultMark.VOID: Decimal(0),
    }
    return by_mark[view.result]


def day_summary(day_rows: Sequence[SignalOutcome], day: date) -> DaySummary:
    """Summary of the signals of one local day."""
    marks = [row.view.result for row in day_rows]
    clvs = [row.clv for row in day_rows if row.clv is not None]
    return DaySummary(
        day=day,
        signals=len(day_rows),
        won=sum(mark in _WINS for mark in marks),
        lost=sum(mark in _LOSSES for mark in marks),
        void=marks.count(ResultMark.VOID),
        pending=marks.count(None),
        profit_units=_total(row.profit_units for row in day_rows),
        clv=_mean(clvs),
    )


def build_report(
    period: tuple[date | None, date],
    group_by: ReportGroup | None,
    rows: Sequence[SignalOutcome],
    bets: tuple[Decimal, Decimal],
) -> ReportView:
    """Report over ``rows`` (already filtered to ``period``); ``bets`` = (profit, stake)."""
    settled = [row for row in rows if row.view.result is not None]
    marks = [row.view.result for row in settled]
    won = sum(mark in _WINS for mark in marks)
    lost = sum(mark in _LOSSES for mark in marks)
    profit = _total(row.profit_units for row in settled)
    staked = _staked(settled)
    clvs = [row.clv for row in settled if row.clv is not None]
    bet_profit, bet_stake = bets
    date_from, date_to = period
    return ReportView(
        date_from=date_from,
        date_to=date_to,
        signals=len(rows),
        settled=len(settled),
        won=won,
        lost=lost,
        void=marks.count(ResultMark.VOID),
        profit_units=profit,
        yield_rate=float(profit / staked) if staked else None,
        roi=float(bet_profit / bet_stake) if bet_stake else None,
        clv=_mean(clvs),
        clv_beat_rate=_mean([1.0 if clv > 0 else 0.0 for clv in clvs]),
        win_rate=won / (won + lost) if won + lost else None,
        longest_losing_streak=_longest_losing_streak(settled),
        calibration=_calibration(settled),
        segments=_segments(settled, group_by),
    )


def _total(values: Iterable[Decimal | None]) -> Decimal:
    return sum((value for value in values if value is not None), Decimal(0))


def _staked(settled: Sequence[SignalOutcome]) -> Decimal:
    """Units at risk: void signals returned the stake, so they do not count."""
    return _total(row.view.stake_units for row in settled if row.view.result != ResultMark.VOID)


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _longest_losing_streak(settled: Sequence[SignalOutcome]) -> int:
    longest = current = 0
    for row in sorted(settled, key=lambda item: (item.view.kickoff, item.view.signal_id)):
        current = current + 1 if row.view.result in _LOSSES else 0
        longest = max(longest, current)
    return longest


def _calibration(settled: Sequence[SignalOutcome]) -> tuple[CalibrationBucket, ...]:
    decided = [row.view for row in settled if row.view.result in _WINS | _LOSSES]
    buckets = []
    for lower, upper in itertools.pairwise(_CALIBRATION_EDGES):
        inside = [view for view in decided if lower <= view.probability < upper]
        if inside:
            buckets.append(
                CalibrationBucket(
                    lower=lower,
                    upper=upper,
                    count=len(inside),
                    predicted=sum(view.probability for view in inside) / len(inside),
                    actual=sum(view.result in _WINS for view in inside) / len(inside),
                )
            )
    return tuple(buckets)


_GROUP_KEYS: Final[dict[ReportGroup, Callable[[SignalOutcome], str]]] = {
    ReportGroup.SPORT: lambda row: row.view.sport.value,
    ReportGroup.LEAGUE: lambda row: row.view.league,
    ReportGroup.MARKET: lambda row: row.view.market.type.value,
    ReportGroup.RISK_PROFILE: lambda row: row.risk_profile.value,
    ReportGroup.CONFIDENCE: lambda row: row.view.confidence.value,
}


def _segments(
    settled: Sequence[SignalOutcome], group_by: ReportGroup | None
) -> tuple[SegmentStat, ...]:
    if group_by is None:
        return ()
    key_of = _GROUP_KEYS[group_by]
    groups: dict[str, list[SignalOutcome]] = {}
    for row in settled:
        groups.setdefault(key_of(row), []).append(row)
    return tuple(_segment(key, groups[key]) for key in sorted(groups))


def _segment(key: str, rows: Sequence[SignalOutcome]) -> SegmentStat:
    marks = [row.view.result for row in rows]
    won = sum(mark in _WINS for mark in marks)
    decided = won + sum(mark in _LOSSES for mark in marks)
    profit = _total(row.profit_units for row in rows)
    staked = _staked(rows)
    return SegmentStat(
        key=key,
        signals=len(rows),
        profit_units=profit,
        yield_rate=float(profit / staked) if staked else None,
        clv=_mean([row.clv for row in rows if row.clv is not None]),
        win_rate=won / decided if decided else None,
    )
