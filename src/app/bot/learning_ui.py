"""Pure parts of ``/ozgarishlar`` and the weekly summary (plan 6.13 C, F8.8).

The journal is shown newest first. A shadow-mode entry says what the model *would* have done;
only a real, standing entry gets an undo button, and undoing asks for confirmation first
(it changes what the analysis does for everybody). Journal fields come from the pipeline
(segment keys, reason codes) and are escaped.
"""

from collections.abc import Sequence
from typing import Final
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot import texts
from app.bot.callbacks import LearnCb
from app.bot.formatters import escape, percent, short_date
from app.bot.keyboards import markup_of, single_button
from app.bot.paging import Page, pager_row
from app.services.interfaces import (
    LearningChangeKind,
    LearningChangeView,
    LearningMode,
    LearningPage,
    LearningSummary,
    RollbackOutcome,
)

MAX_SUMMARY_LINES: Final = 8
"""Entries spelled out in the weekly message; the rest is summarised in the counts."""
_CALIBRATION_KINDS: Final = frozenset(
    {LearningChangeKind.CALIBRATION_ACTIVATED, LearningChangeKind.CALIBRATION_ROLLED_BACK}
)


def _detail(change: LearningChangeView) -> str:
    samples = (
        "" if change.samples is None else texts.LEARN_DETAIL_SAMPLES.format(samples=change.samples)
    )
    if change.kind in _CALIBRATION_KINDS:
        return texts.LEARN_DETAIL_CALIBRATION.format(
            version="?" if change.version is None else change.version, samples=samples
        )
    floor = (
        ""
        if change.ev_floor is None
        else texts.LEARN_DETAIL_FLOOR.format(floor=percent(change.ev_floor, decimals=1))
    )
    return texts.LEARN_DETAIL_SEGMENT.format(
        subject=escape(change.subject), samples=samples, floor=floor
    )


def _state(change: LearningChangeView) -> str:
    if change.rolled_back:
        return texts.LEARN_STATE_ROLLED_BACK
    return texts.LEARN_STATE_APPLIED if change.applied else texts.LEARN_STATE_SHADOW


def format_change(change: LearningChangeView, zone: ZoneInfo) -> str:
    """One journal entry: kind and date, subject and numbers, reason and whether it stands."""
    day = short_date(change.at.astimezone(zone).date())
    return "\n".join(
        [
            texts.LEARN_LINE.format(
                id=change.change_id, kind=texts.LEARN_KINDS[change.kind.value], date=day
            ),
            _detail(change),
            texts.LEARN_REASON.format(reason=escape(change.reason)),
            _state(change),
        ]
    )


def format_journal(page: LearningPage, zone: ZoneInfo) -> str:
    """The list screen: the mode, then the entries of the page (or the empty state)."""
    lines = [texts.LEARN_TITLE.format(total=page.total), texts.LEARN_MODES[page.mode.value]]
    if not page.items:
        lines.extend(["", texts.LEARN_EMPTY])
        return "\n".join(lines)
    for change in page.items:
        lines.extend(["", format_change(change, zone)])
    return "\n".join(lines)


def journal_keyboard(page: LearningPage) -> InlineKeyboardMarkup | None:
    """An undo button per standing entry of the page, then the pager."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            single_button(
                texts.BTN_LEARN_UNDO.format(id=change.change_id),
                LearnCb(action="ask", id=change.change_id, page=page.page),
            )
        ]
        for change in page.items
        if change.undoable
    ]
    pager = pager_row(
        Page((), page.page, page.page_count), lambda number: LearnCb(action="list", page=number)
    )
    if pager:
        rows.append(pager)
    return markup_of(rows) if rows else None


def format_undo_ask(change: LearningChangeView, zone: ZoneInfo, hold_days: int) -> str:
    """The confirmation screen: the entry itself and what undoing it means."""
    return texts.LEARN_UNDO_ASK.format(
        id=change.change_id, line=format_change(change, zone), days=hold_days
    )


def undo_keyboard(change_id: int, page: int) -> InlineKeyboardMarkup:
    """Confirm or go back to the list page."""
    return markup_of(
        [
            [
                single_button(
                    texts.BTN_LEARN_UNDO_YES, LearnCb(action="undo", id=change_id, page=page)
                ),
                single_button(texts.BTN_LEARN_UNDO_NO, LearnCb(action="list", page=page)),
            ]
        ]
    )


def back_keyboard(page: int) -> InlineKeyboardMarkup:
    """Only the way back to the list page."""
    return markup_of([[single_button(texts.BTN_LEARN_BACK, LearnCb(action="list", page=page))]])


def undo_outcome_text(outcome: RollbackOutcome, hold_days: int) -> str:
    """What happened when the user asked to undo an entry."""
    return texts.LEARN_UNDO_OUTCOMES[outcome.value].format(days=hold_days)


def _count(changes: Sequence[LearningChangeView], *kinds: LearningChangeKind) -> int:
    return sum(change.kind in kinds for change in changes)


def format_summary(summary: LearningSummary, zone: ZoneInfo) -> str:
    """The weekly "what the model learned" message (counts, then the entries)."""
    lines = [
        texts.LEARN_SUMMARY_TITLE.format(week=short_date(summary.week)),
        texts.LEARN_MODES[summary.mode.value],
    ]
    changes = summary.changes
    if not changes:
        lines.extend(["", texts.LEARN_SUMMARY_NONE])
    else:
        lines.extend(
            [
                "",
                texts.LEARN_SUMMARY_COUNTS.format(
                    calibration=_count(changes, LearningChangeKind.CALIBRATION_ACTIVATED),
                    disabled=_count(changes, LearningChangeKind.SEGMENT_DISABLED),
                    raised=_count(changes, LearningChangeKind.SEGMENT_RAISED),
                    relaxed=_count(changes, LearningChangeKind.SEGMENT_RELAXED),
                ),
            ]
        )
        for change in changes[:MAX_SUMMARY_LINES]:
            lines.extend(["", format_change(change, zone)])
        if len(changes) > MAX_SUMMARY_LINES:
            lines.append(texts.LEARN_SUMMARY_MORE.format(count=len(changes) - MAX_SUMMARY_LINES))
    if summary.mode is not LearningMode.OFF:
        lines.extend(["", texts.LEARN_SUMMARY_FOOTER])
    return "\n".join(lines)
