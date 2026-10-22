"""Pure parts of 📈 Hisobot (plan §6.10, F7.6): the report text, tables and period/slice keyboard.

All numbers come from the service (``ReportView``); this module only lays them out. Slice
keys that are enum values (sport, market, risk profile, confidence) are shown in Uzbek; league
names and everything else is escaped. Tables are ``<pre>`` blocks, so every cell is escaped
and padded to a fixed width.
"""

from collections.abc import Callable, Sequence
from datetime import date
from typing import Final

from aiogram.types import InlineKeyboardMarkup

from app.bot import texts
from app.bot.callbacks import ReportCb
from app.bot.draft import StaleDraftError
from app.bot.formatters import escape, percent, short_date, signed_units, sport_label
from app.bot.keyboards import checked_label, markup_of, single_button
from app.domain import MarketType, Sport
from app.services.interfaces import (
    CalibrationBucket,
    ConfidenceLevel,
    ReportGroup,
    ReportRequest,
    ReportView,
    RiskProfile,
    SegmentStat,
)

PERIOD_CUSTOM: Final = "c"
PERIOD_ALL: Final = "all"
RECENT_PERIODS: Final = {"7": 7, "30": 30}
MAX_SEGMENT_ROWS: Final = 15
LABEL_WIDTH: Final = 24
MIN_CALIBRATION_SAMPLE: Final = 10
"""Fewer closed signals in a bucket say nothing about calibration; the sentence is withheld."""
GROUP_CODES: Final[dict[str, ReportGroup | None]] = {
    "": None,
    **{group.value: group for group in ReportGroup},
}


def build_request(
    period: str, group: ReportGroup | None, custom: tuple[date, date] | None
) -> ReportRequest:
    """The service request a period button names.

    Raises:
        StaleDraftError: unknown period, or a custom period whose dates are gone.
    """
    if period in RECENT_PERIODS:
        return ReportRequest.recent(RECENT_PERIODS[period], group_by=group)
    if period == PERIOD_ALL:
        return ReportRequest.all_time(group_by=group)
    if period == PERIOD_CUSTOM and custom is not None:
        return ReportRequest(date_from=custom[0], date_to=custom[1], group_by=group)
    raise StaleDraftError


def period_label(view: ReportView) -> str:
    """A date range, or "hammasi (21.10 gacha)" for the whole history."""
    end = short_date(view.date_to)
    if view.date_from is None:
        return texts.REPORT_PERIOD_ALL.format(end=end)
    return texts.REPORT_PERIOD_RANGE.format(start=short_date(view.date_from), end=end)


def _optional_percent(value: float | None, *, signed: bool = False, decimals: int = 1) -> str:
    return texts.REPORT_NONE if value is None else percent(value, signed=signed, decimals=decimals)


_SEGMENT_LABELS: Final[dict[ReportGroup, Callable[[str], str]]] = {
    ReportGroup.SPORT: lambda key: sport_label(Sport(key)),
    ReportGroup.MARKET: lambda key: texts.MARKET_LABELS[MarketType(key)],
    ReportGroup.RISK_PROFILE: lambda key: texts.RISK_LABELS[RiskProfile(key)],
    ReportGroup.CONFIDENCE: lambda key: texts.CONFIDENCE_LABELS[ConfidenceLevel(key)],
    ReportGroup.LEAGUE: lambda key: key,
}


def segment_label(group: ReportGroup, key: str) -> str:
    """A slice key as the user should read it (enum values translated, the rest as is)."""
    try:
        return _SEGMENT_LABELS[group](key)
    except (ValueError, KeyError):  # a value the bot does not know (older service): show it raw
        return key


def _cell(text: str, width: int) -> str:
    """Escaped ``text`` cut to ``width`` characters (counted before escaping) and padded."""
    cut = text if len(text) <= width else text[: width - 1] + "…"
    return escape(cut) + " " * (width - len(cut))


def calibration_rows(buckets: Sequence[CalibrationBucket]) -> list[str]:
    """One ``<pre>`` line per bucket: range, count, promised and actual probability."""
    rows = []
    for bucket in buckets:
        span = f"{bucket.lower * 100:.0f}{texts.RANGE_DASH}{bucket.upper * 100:.0f}%"
        rows.append(
            f"{span:<17} {bucket.count:>3}  {percent(bucket.predicted):>6}  "
            f"{percent(bucket.actual):>7}"
        )
    return rows


def calibration_block(buckets: Sequence[CalibrationBucket]) -> list[str]:
    """The calibration table and one plain sentence about the best-covered bucket."""
    if not buckets:
        return []
    lines = [
        texts.REPORT_CALIBRATION_HEADER,
        "<pre>"
        + "\n".join([texts.REPORT_CALIBRATION_COLUMNS, *calibration_rows(buckets)])
        + "</pre>",
    ]
    largest = max(buckets, key=lambda bucket: bucket.count)
    if largest.count >= MIN_CALIBRATION_SAMPLE:
        lines.append(
            texts.REPORT_CALIBRATION_SENTENCE.format(
                lower=f"{largest.lower * 100:.0f}",
                upper=f"{largest.upper * 100:.0f}",
                actual=percent(largest.actual),
                count=largest.count,
            )
        )
    else:
        lines.append(texts.REPORT_CALIBRATION_THIN.format(count=sum(b.count for b in buckets)))
    return lines


def _segment_row(group: ReportGroup, segment: SegmentStat) -> str:
    label = _cell(segment_label(group, segment.key), LABEL_WIDTH)
    profit = signed_units(segment.profit_units)
    return (
        f"{label} {segment.signals:>3} {profit:>7} "
        f"{_optional_percent(segment.yield_rate, signed=True):>6} "
        f"{_optional_percent(segment.clv, signed=True):>6} "
        f"{_optional_percent(segment.win_rate, decimals=0):>4}"
    )


def segments_block(group: ReportGroup | None, segments: Sequence[SegmentStat]) -> list[str]:
    """The slice table (nothing without a slice or without data)."""
    if group is None or not segments:
        return []
    shown = segments[:MAX_SEGMENT_ROWS]
    rows = [texts.REPORT_SEGMENTS_COLUMNS, *(_segment_row(group, segment) for segment in shown)]
    lines = [
        texts.REPORT_SEGMENTS_HEADER.format(group=texts.REPORT_GROUP_LABELS[group.value]),
        "<pre>" + "\n".join(rows) + "</pre>",
    ]
    if len(segments) > len(shown):
        lines.append(texts.REPORT_MORE.format(count=len(segments) - len(shown)))
    return lines


def _summary_lines(view: ReportView) -> list[str]:
    roi = (
        texts.REPORT_ROI_NONE
        if view.roi is None
        else texts.REPORT_ROI.format(roi=percent(view.roi, signed=True, decimals=1))
    )
    return [
        texts.REPORT_COUNTS.format(
            signals=view.signals,
            settled=view.settled,
            won=view.won,
            lost=view.lost,
            void=view.void,
        ),
        texts.REPORT_PROFIT.format(
            profit=signed_units(view.profit_units),
            yield_rate=_optional_percent(view.yield_rate, signed=True),
        ),
        roi,
        texts.REPORT_CLV.format(
            clv=_optional_percent(view.clv, signed=True),
            beat=_optional_percent(view.clv_beat_rate, decimals=0),
        ),
        texts.REPORT_WINRATE.format(
            rate=_optional_percent(view.win_rate, decimals=0), streak=view.longest_losing_streak
        ),
    ]


def format_report(view: ReportView, group: ReportGroup | None) -> str:
    """The report: headline numbers, the calibration table and the optional slice table."""
    lines = [texts.REPORT_TITLE.format(period=period_label(view))]
    if view.signals == 0:
        return "\n".join([*lines, texts.REPORT_EMPTY])
    lines.extend(_summary_lines(view))
    if view.settled == 0:
        lines.append(texts.REPORT_NO_SETTLED)
    blocks = [calibration_block(view.calibration), segments_block(group, view.segments)]
    for block in blocks:
        if block:
            lines.extend(["", *block])
    return "\n".join(lines)


def report_keyboard(period: str, group: ReportGroup | None) -> InlineKeyboardMarkup:
    """Period row, slice rows, chart and export."""
    code = "" if group is None else group.value
    periods = [
        single_button(
            checked_label(label, on=key == period),
            ReportCb(action="custom" if key == PERIOD_CUSTOM else "show", period=key, group=code),
        )
        for key, label in texts.BTN_REPORT_PERIODS.items()
    ]
    groups = [
        single_button(
            checked_label(texts.REPORT_GROUP_LABELS[value], on=value == code),
            ReportCb(action="show", period=period, group=value),
        )
        for value in GROUP_CODES
    ]
    return markup_of(
        [
            periods,
            groups[:3],
            groups[3:],
            [
                single_button(
                    texts.BTN_REPORT_CHART, ReportCb(action="chart", period=period, group=code)
                ),
                single_button(texts.BTN_REPORT_EXPORT, ReportCb(action="export")),
            ],
        ]
    )
