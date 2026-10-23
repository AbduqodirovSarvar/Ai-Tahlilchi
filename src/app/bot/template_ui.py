"""Pure parts of 💾 Shablonlarim (plan §6.2, F7.4): list, template screen and schedule editor.

A saved template keeps its dates relative ("ertaga", "3 kun"), so running it later analyses
the new period. The schedule (ISO weekdays plus a local time) is stored with the template;
the editor state is a small JSON-safe draft kept in FSM data.
"""

from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import time
from typing import Any, Final, Self

from aiogram.types import InlineKeyboardMarkup

from app.bot import texts
from app.bot.callbacks import TplCb
from app.bot.draft import StaleDraftError, WizardDraft
from app.bot.formatters import clock, escape, short_date
from app.bot.keyboards import (
    SETTINGS_SEND_TIME_CHOICES,
    checked_label,
    markup_of,
    single_button,
)
from app.bot.paging import pager_row, paginate
from app.bot.wizard import summary_fields
from app.services.interfaces import DateMode, DateSpec, TemplateSchedule, TemplateView

WEEKDAYS: Final = (1, 2, 3, 4, 5, 6, 7)
DEFAULT_WEEKDAYS: Final = (1, 2, 3, 4, 5)
"""A new schedule starts on working days; the user toggles from there."""


def date_label(spec: DateSpec) -> str:
    """``bugun``, ``3 kun`` or a date range: how a template names its (relative) dates."""
    template = texts.TPL_DATES[spec.mode.value]
    if spec.mode is DateMode.NEXT_DAYS:
        return template.format(days=spec.days)
    if spec.mode is DateMode.RANGE and spec.date_from is not None and spec.date_to is not None:
        return template.format(start=short_date(spec.date_from), end=short_date(spec.date_to))
    return template


def days_label(weekdays: Sequence[int]) -> str:
    """``Du Se Ch`` in weekday order."""
    names = [texts.WEEKDAYS_SHORT[day - 1] for day in sorted(set(weekdays))]
    return " ".join(names) or texts.TPL_NO_DAYS


def schedule_label(schedule: TemplateSchedule | None) -> str:
    """``Du Ch 09:00`` or "yo'q"."""
    if schedule is None:
        return texts.TPL_NO_SCHEDULE
    return texts.TPL_SCHEDULE_TEXT.format(
        days=days_label(sorted(schedule.weekdays)), time=clock(schedule.at)
    )


def _draft_of(view: TemplateView) -> WizardDraft:
    """The template's query as a wizard draft with neutral scope labels (display only)."""
    return WizardDraft().with_query(view.query, {})


def summary_of(view: TemplateView) -> str:
    """One-line summary used in the list."""
    fields = summary_fields(_draft_of(view), date_label(view.query.dates))
    return texts.TPL_SUMMARY.format(
        dates=fields["window"],
        sports=fields["sports"],
        probability=fields["probability"],
        ev=fields["ev"],
    )


def format_templates(views: Sequence[TemplateView], page: int = 0) -> str:
    """The list screen (one page)."""
    if not views:
        return texts.TPL_EMPTY
    lines = [texts.TPL_TITLE.format(count=len(views))]
    for view in paginate(views, page).items:
        schedule = (
            ""
            if view.schedule is None
            else texts.TPL_SCHEDULE_SHORT.format(
                days=days_label(sorted(view.schedule.weekdays)), time=clock(view.schedule.at)
            )
        )
        lines.append(
            texts.TPL_LINE.format(
                name=escape(view.name), summary=summary_of(view), schedule=schedule
            )
        )
    return "\n".join(lines)


def templates_keyboard(views: Sequence[TemplateView], page: int = 0) -> InlineKeyboardMarkup:
    """One button per template of the page and the pager (``value`` = page number)."""
    current = paginate(views, page)
    rows = [
        [
            single_button(
                ("🗓 " if view.schedule else "💾 ") + view.name,
                TplCb(action="open", id=view.template_id, value=current.number),
            )
        ]
        for view in current.items
    ]
    pager = pager_row(current, lambda number: TplCb(action="list", value=number))
    if pager:
        rows.append(pager)
    return markup_of(rows)


def format_template(view: TemplateView) -> str:
    """The template's screen: every parameter of its query and the schedule."""
    fields = summary_fields(_draft_of(view), date_label(view.query.dates))
    return texts.TPL_DETAIL.format(
        name=escape(view.name),
        dates=fields["window"],
        sports=fields["sports"],
        probability=fields["probability"],
        ev=fields["ev"],
        scope=fields["scope"],
        markets=fields["markets"],
        count=fields["count"],
        sort=fields["sort"],
        schedule=schedule_label(view.schedule),
    )


def template_keyboard(view: TemplateView, page: int = 0) -> InlineKeyboardMarkup:
    """Run, schedule, delete and back (to the list page the template was opened from)."""
    template_id = view.template_id
    return markup_of(
        [
            [single_button(texts.BTN_TPL_RUN, TplCb(action="run", id=template_id))],
            [
                single_button(texts.BTN_TPL_SCHEDULE, TplCb(action="schedule", id=template_id)),
                single_button(
                    texts.BTN_TPL_DELETE, TplCb(action="delete", id=template_id, value=page)
                ),
            ],
            [single_button(texts.BTN_TPL_BACK, TplCb(action="list", value=page))],
        ]
    )


def delete_keyboard(template_id: int, page: int = 0) -> InlineKeyboardMarkup:
    """The delete confirmation ("no" returns to the template on the same list page)."""
    return markup_of(
        [
            [
                single_button(
                    texts.BTN_TPL_DELETE_YES, TplCb(action="delete_yes", id=template_id, value=page)
                ),
                single_button(
                    texts.BTN_TPL_DELETE_NO, TplCb(action="open", id=template_id, value=page)
                ),
            ]
        ]
    )


# --------------------------------------------------------------------------- schedule editor
@dataclass(frozen=True, slots=True, kw_only=True)
class ScheduleDraft:
    """The schedule being edited: ISO weekdays and a local time ``HH:MM``."""

    message_id: int | None
    template_id: int
    weekdays: tuple[int, ...]
    at: str

    @classmethod
    def starting(cls, view: TemplateView, message_id: int | None, default_at: time) -> Self:
        """The template's schedule, or working days at the user's digest time."""
        schedule = view.schedule
        return cls(
            message_id=message_id,
            template_id=view.template_id,
            weekdays=tuple(sorted(schedule.weekdays)) if schedule else DEFAULT_WEEKDAYS,
            at=clock(schedule.at if schedule else default_at),
        )

    def to_data(self) -> dict[str, Any]:
        """JSON-safe form for FSM storage."""
        return asdict(self)

    @classmethod
    def from_data(cls, data: object) -> Self:
        """Decode, or raise :class:`StaleDraftError` when missing or corrupt."""
        if not isinstance(data, dict):
            raise StaleDraftError
        try:
            draft = cls(
                message_id=data["message_id"],
                template_id=int(data["template_id"]),
                weekdays=tuple(int(day) for day in data["weekdays"]),
                at=str(data["at"]),
            )
            _ = draft.local_time
        except (KeyError, TypeError, ValueError):
            raise StaleDraftError from None
        if not all(day in WEEKDAYS for day in draft.weekdays):
            raise StaleDraftError
        return draft

    @property
    def local_time(self) -> time:
        """The chosen local time."""
        hours, minutes = self.at.split(":")
        return time(int(hours), int(minutes))

    def toggled(self, weekday: int) -> Self:
        """The draft with ``weekday`` switched on or off.

        Raises:
            StaleDraftError: not a weekday number.
        """
        if weekday not in WEEKDAYS:
            raise StaleDraftError
        chosen = set(self.weekdays) ^ {weekday}
        return replace(self, weekdays=tuple(sorted(chosen)))

    def at_time(self, value: time) -> Self:
        """The draft at another time."""
        return replace(self, at=clock(value))

    def schedule(self) -> TemplateSchedule | None:
        """The schedule to save, or ``None`` when no weekday is chosen."""
        if not self.weekdays:
            return None
        return TemplateSchedule(weekdays=frozenset(self.weekdays), at=self.local_time)


def format_schedule(view: TemplateView, draft: ScheduleDraft, zone_name: str) -> str:
    """The editor's text: current choice and the note about automatic runs."""
    title = texts.TPL_SCHEDULE_TITLE.format(
        name=escape(view.name),
        zone=escape(zone_name),
        days=days_label(draft.weekdays),
        time=draft.at,
    )
    return f"{title}\n\n{texts.TPL_SCHEDULE_NOTE}"


def schedule_keyboard(draft: ScheduleDraft) -> InlineKeyboardMarkup:
    """Weekday switches, time presets, typed time, save and clear."""
    chosen = set(draft.weekdays)
    template_id = draft.template_id
    days = [
        single_button(
            checked_label(texts.WEEKDAYS_SHORT[day - 1], on=day in chosen),
            TplCb(action="day", id=template_id, value=day),
        )
        for day in WEEKDAYS
    ]
    times = [
        single_button(
            (f"✅ {clock(value)}" if clock(value) == draft.at else clock(value)),
            TplCb(action="time", id=template_id, value=index),
        )
        for index, value in enumerate(SETTINGS_SEND_TIME_CHOICES)
    ]
    return markup_of(
        [
            days[:4],
            days[4:],
            times[:3],
            times[3:],
            [single_button(texts.BTN_TPL_TYPE_TIME, TplCb(action="typed", id=template_id))],
            [
                single_button(texts.BTN_TPL_SAVE_SCHEDULE, TplCb(action="save", id=template_id)),
                single_button(texts.BTN_TPL_CLEAR_SCHEDULE, TplCb(action="clear", id=template_id)),
            ],
            [single_button(texts.BTN_TPL_BACK, TplCb(action="open", id=template_id))],
        ]
    )
