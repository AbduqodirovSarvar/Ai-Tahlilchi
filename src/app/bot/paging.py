"""Pagination of the list screens (watchlist, templates): the slice and the ``◀️ n/m ▶️`` row.

A list must never hide entries behind a "…and N more" line: every entry has a button on some
page. Pages are clamped, so a button of a page that no longer exists (entries were removed
meanwhile) shows the last page instead of failing.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton

from app.bot import texts
from app.bot.keyboards import single_button

PAGE_SIZE: Final = 10
"""Entries per page: ten buttons plus the pager and the add buttons stay well within limits."""


@dataclass(frozen=True, slots=True)
class Page[T]:
    """One page of a list."""

    items: tuple[T, ...]
    number: int
    count: int

    @property
    def paged(self) -> bool:
        """True when there is more than one page."""
        return self.count > 1


def paginate[T](items: Sequence[T], page: int, size: int = PAGE_SIZE) -> Page[T]:
    """The ``page`` (0-based, clamped into range) of ``items``; an empty list has one page."""
    count = max(1, -(-len(items) // size))
    number = min(max(page, 0), count - 1)
    return Page(tuple(items[number * size : (number + 1) * size]), number, count)


def pager_row(
    page: Page[object], make: Callable[[int], CallbackData]
) -> list[InlineKeyboardButton]:
    """``◀️ n/m ▶️`` (``[]`` for a single page); ``make(n)`` builds the callback of page ``n``."""
    if not page.paged:
        return []
    row: list[InlineKeyboardButton] = []
    if page.number > 0:
        row.append(single_button(texts.BTN_PREV_PAGE, make(page.number - 1)))
    row.append(
        single_button(
            texts.PAGE_INDICATOR.format(page=page.number + 1, pages=page.count), make(page.number)
        )
    )
    if page.number + 1 < page.count:
        row.append(single_button(texts.BTN_NEXT_PAGE, make(page.number + 1)))
    return row
