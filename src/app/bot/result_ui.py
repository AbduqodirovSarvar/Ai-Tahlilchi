"""Pure parts of the result marking (plan 6.13 A, F8.1): the ``/natijalar`` list.

Marks are signal-level (``ResultService.mark_result``); the buttons carry only the signal id,
a mark code and the list page, never names. Everything that comes from outside (team names)
is escaped.
"""

from collections.abc import Sequence
from typing import Final
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot import texts
from app.bot.callbacks import ResultCb
from app.bot.formatters import clock, escape, odds, selection_label, short_date
from app.bot.keyboards import markup_of, single_button
from app.bot.paging import pager_row, paginate
from app.bot.result_buttons import ORIGIN_LIST, mark_row, note_button
from app.services.interfaces import SignalView

PENDING_PAGE_SIZE: Final = 6
"""Signals per page of ``/natijalar``: two button rows each, so six signals stay compact."""


def format_pending(signals: Sequence[SignalView], zone: ZoneInfo, page: int = 0) -> str:
    """The list screen (one page) of signals waiting for a result."""
    if not signals:
        return texts.RESULTS_PENDING_EMPTY
    current = paginate(signals, page, PENDING_PAGE_SIZE)
    first = current.number * PENDING_PAGE_SIZE
    lines = [texts.RESULTS_PENDING_COMMAND_TITLE.format(count=len(signals)), ""]
    for offset, signal in enumerate(current.items, start=1):
        kickoff = signal.kickoff.astimezone(zone)
        lines.append(
            texts.RESULTS_PENDING_LINE.format(
                number=first + offset,
                fixture=escape(signal.fixture_label),
                selection=selection_label(signal),
                odds=odds(signal.odds),
                kickoff=f"{short_date(kickoff.date())} {clock(kickoff.time())}",
            )
        )
    lines.extend(["", texts.RESULTS_PENDING_HINT])
    return "\n".join(lines)


def pending_keyboard(signals: Sequence[SignalView], page: int = 0) -> InlineKeyboardMarkup | None:
    """Per signal of the page: its number as a caption row, the mark row and 💬 (one tap each)."""
    if not signals:
        return None
    current = paginate(signals, page, PENDING_PAGE_SIZE)
    first = current.number * PENDING_PAGE_SIZE
    rows: list[list[InlineKeyboardButton]] = []
    for offset, signal in enumerate(current.items, start=1):
        marks = mark_row(signal.signal_id, page=current.number, origin=ORIGIN_LIST)
        note = note_button(signal.signal_id, page=current.number, origin=ORIGIN_LIST)
        label = single_button(f"{first + offset}.", ResultCb(action="list", page=current.number))
        rows.append([label, *marks[:2]])
        rows.append([*marks[2:], note])
    pager = pager_row(current, lambda number: ResultCb(action="list", page=number))
    if pager:
        rows.append(pager)
    return markup_of(rows)
