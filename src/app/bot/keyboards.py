"""Keyboard builders (pure: data in, aiogram markup out) and the preset choices they offer."""

import calendar
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, time, timedelta
from typing import Final

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.bot import texts
from app.bot.callbacks import (
    CalendarCb,
    CardCb,
    ConfirmCb,
    CountCb,
    DateCb,
    EditCb,
    EvCb,
    MarketCb,
    NavCb,
    NoopCb,
    PickCb,
    ProbabilityCb,
    ResultsCb,
    ScopeCb,
    SettingsCb,
    SortCb,
    SportCb,
)
from app.domain import MarketType
from app.services.interfaces import (
    MAX_RETRO_LOOKBACK_DAYS,
    MAX_WINDOW_DAYS,
    RISK_PRESETS,
    RiskProfile,
    ScopeKind,
    SortKey,
    SportOption,
    horizon_end,
)

type Rows = list[list[InlineKeyboardButton]]

# --------------------------------------------------------------------------- presets
WIZARD_PROBABILITY_CHOICES: Final = (50, 60, 70, 80, 90)
WIZARD_EV_CHOICES: Final = (0, 3, 5, 8, 10)
WIZARD_COUNT_CHOICES: Final = (3, 5, 10, 15)
NEXT_DAYS_CHOICES: Final = (3, 7)
CUSTOM: Final = -1
"""Button value meaning "let me type it"."""
COUNT_ALL: Final = 0
COUNT_TOGGLE_PER_DAY: Final = -2

SETTINGS_MAX_SIGNALS_CHOICES: Final = (3, 5, 10, 15, 20, 30)
SETTINGS_PROBABILITY_CHOICES: Final = (0.30, 0.40, 0.45, 0.50, 0.55, 0.60, 0.70, 0.80, 0.90)
SETTINGS_EV_CHOICES: Final = (0.0, 0.03, 0.05, 0.08, 0.10, 0.15)
SETTINGS_ODDS_CHOICES: Final = ((1.30, 2.00), (1.40, 3.00), (1.50, 3.00), (2.00, 5.00))
SETTINGS_SEND_TIME_CHOICES: Final = (time(7), time(8), time(9), time(10), time(12), time(18))
SETTINGS_PREMATCH_CHOICES: Final = (None, 30, 60, 90)
SETTINGS_QUIET_CHOICES: Final = (
    None,
    (time(0), time(8)),
    (time(23), time(7)),
    (time(22), time(8)),
)
SETTINGS_TIMEZONE_CHOICES: Final = (
    "Asia/Tashkent",
    "Asia/Almaty",
    "Europe/Moscow",
    "Europe/Istanbul",
    "Europe/London",
    "UTC",
)
SETTINGS_RISK_CHOICES: Final = tuple(RISK_PRESETS)
SETTINGS_FIELDS: Final = tuple(texts.SETTINGS_BUTTONS)
MULTI_SELECT_FIELDS: Final = frozenset({"sports", "markets", "leagues"})
MONEY_FIELDS: Final = frozenset({"bank", "unit", "daily_loss", "weekly_loss"})
TYPED_FIELDS: Final = frozenset(texts.SETTINGS_INPUT_PROMPTS)
OFF_FIELDS: Final = MONEY_FIELDS | {"quiet"}


def _button(text: str, callback: object) -> InlineKeyboardButton:
    pack = getattr(callback, "pack")  # noqa: B009 - every CallbackData has `pack`
    return InlineKeyboardButton(text=text, callback_data=pack())


def _markup(rows: Rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[row for row in rows if row])


def _chunks[T](items: Sequence[T], size: int) -> list[list[T]]:
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def nav_row(*, default: bool = False) -> list[InlineKeyboardButton]:
    """``⬅️ Orqaga`` [``⏭ Standart``] ``❌ Bekor qilish`` — on every wizard step."""
    row = [_button(texts.BTN_BACK, NavCb(action="back"))]
    if default:
        row.append(_button(texts.BTN_DEFAULT, NavCb(action="default")))
    row.append(_button(texts.BTN_CANCEL, NavCb(action="cancel")))
    return row


def nav_keyboard(*, default: bool = False) -> InlineKeyboardMarkup:
    """Only the navigation row (text-input steps)."""
    return _markup([nav_row(default=default)])


def _checked(label: str, *, on: bool) -> str:
    return f"{texts.CHECKED if on else texts.UNCHECKED} {label}"


# --------------------------------------------------------------------------- main menu
MAIN_MENU_LAYOUT: Final = (
    (texts.MENU_ANALYSIS, texts.MENU_CUSTOM),
    (texts.MENU_TEMPLATES, texts.MENU_TODAY),
    (texts.MENU_WATCHLIST, texts.MENU_BETS),
    (texts.MENU_REPORT, texts.MENU_SETTINGS),
    (texts.MENU_HELP,),
)


MENU_TEXTS: Final = frozenset(label for row in MAIN_MENU_LAYOUT for label in row)
"""Menu button texts: never treated as typed input of a flow."""


def main_menu() -> ReplyKeyboardMarkup:
    """Persistent reply keyboard of plan §6.1."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=label) for label in row] for row in MAIN_MENU_LAYOUT],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=texts.MENU_PLACEHOLDER_HINT,
    )


# --------------------------------------------------------------------------- wizard steps
def dates_keyboard() -> InlineKeyboardMarkup:
    """Step 1: quick date choices and the calendar."""
    days = [
        _button(texts.BTN_NEXT_DAYS.format(days=n), DateCb(choice="days", days=n))
        for n in NEXT_DAYS_CHOICES
    ]
    return _markup(
        [
            [
                _button(texts.BTN_TODAY, DateCb(choice="today")),
                _button(texts.BTN_TOMORROW, DateCb(choice="tomorrow")),
            ],
            [_button(texts.BTN_WEEKEND, DateCb(choice="weekend")), *days],
            [_button(texts.BTN_CALENDAR, DateCb(choice="calendar"))],
            nav_row(),
        ]
    )


def month_key(day: date) -> int:
    """``2026-10-xx`` → ``202610`` (compact calendar callback value)."""
    return day.year * 100 + day.month


def _shift_month(key: int, delta: int) -> int:
    index = key // 100 * 12 + key % 100 - 1 + delta
    return index // 12 * 100 + index % 12 + 1


def calendar_bounds(today: date) -> tuple[date, date]:
    """First and last selectable day."""
    return today - timedelta(days=MAX_RETRO_LOOKBACK_DAYS), horizon_end(today)


def _day_button(day: date, today: date, start: date | None) -> InlineKeyboardButton:
    first, last = calendar_bounds(today)
    if start is not None:
        # The end must keep the window within what the service accepts.
        first, last = start, min(last, start + timedelta(days=MAX_WINDOW_DAYS - 1))
    if not first <= day <= last:
        return _button("·", CalendarCb(action="noop"))
    label = str(day.day)
    if day == start:
        label = f"[{label}]"
    elif day == today:
        label = f"•{label}"
    return _button(label, CalendarCb(action="day", value=day.toordinal()))


def calendar_keyboard(month: int, today: date, start: date | None) -> InlineKeyboardMarkup:
    """Month grid; days outside ``calendar_bounds`` (or before ``start``) are inert."""
    year, month_number = divmod(month, 100)
    first, last = calendar_bounds(today)
    prev_month, next_month = _shift_month(month, -1), _shift_month(month, 1)
    header = [
        _button(texts.BTN_PREV_PAGE, CalendarCb(action="month", value=prev_month))
        if prev_month >= month_key(first)
        else _button(" ", CalendarCb(action="noop")),
        _button(f"{texts.MONTHS[month_number - 1]} {year}", CalendarCb(action="noop")),
        _button(texts.BTN_NEXT_PAGE, CalendarCb(action="month", value=next_month))
        if next_month <= month_key(last)
        else _button(" ", CalendarCb(action="noop")),
    ]
    weekdays = [_button(name, CalendarCb(action="noop")) for name in texts.WEEKDAYS_SHORT]
    weeks: Rows = []
    for week in calendar.Calendar().monthdatescalendar(year, month_number):
        weeks.append(
            [
                _day_button(day, today, start)
                if day.month == month_number
                else _button(" ", CalendarCb(action="noop"))
                for day in week
            ]
        )
    return _markup([header, weekdays, *weeks, nav_row()])


def sport_label(option: SportOption) -> str:
    """``⚽ Futbol``, with "🧪" for sports still in test mode."""
    label = texts.SPORT_LABELS[option.sport]
    return label + texts.EXPERIMENTAL_MARK if option.experimental else label


def sports_keyboard(options: Sequence[SportOption]) -> InlineKeyboardMarkup:
    """Step 2: one available sport or all of them."""
    buttons = [
        _button(sport_label(option), SportCb(sport=option.sport.value)) for option in options
    ]
    rows = _chunks(buttons, 2)
    rows.append([_button(texts.ALL_SPORTS, SportCb(sport="all"))])
    return _markup([*rows, nav_row()])


def _percent_buttons(
    choices: Iterable[int], default: float, factory: type[ProbabilityCb] | type[EvCb]
) -> Rows:
    default_percent = round(default * 100)
    buttons = [
        _button(
            f"{value}%" + (texts.DEFAULT_MARK if value == default_percent else ""),
            factory(percent=value),
        )
        for value in choices
    ]
    rows = _chunks(buttons, 3)
    rows.append([_button(texts.BTN_CUSTOM, factory(percent=CUSTOM))])
    return rows


def probability_keyboard(default: float) -> InlineKeyboardMarkup:
    """Step 3a: minimal probability."""
    rows = _percent_buttons(WIZARD_PROBABILITY_CHOICES, default, ProbabilityCb)
    return _markup([*rows, nav_row(default=True)])


def ev_keyboard(default: float) -> InlineKeyboardMarkup:
    """Step 3b: minimal EV."""
    rows = _percent_buttons(WIZARD_EV_CHOICES, default, EvCb)
    return _markup([*rows, nav_row(default=True)])


def scope_keyboard(current: ScopeKind) -> InlineKeyboardMarkup:
    """Step 4: scope kind."""
    labels = {
        ScopeKind.ALL: texts.BTN_SCOPE_ALL,
        ScopeKind.LEAGUES: texts.BTN_SCOPE_LEAGUES,
        ScopeKind.TEAMS: texts.BTN_SCOPE_TEAMS,
        ScopeKind.FIXTURES: texts.BTN_SCOPE_FIXTURES,
    }
    buttons = [
        _button(_checked(label, on=kind is current), ScopeCb(kind=kind.value))
        for kind, label in labels.items()
    ]
    return _markup([*_chunks(buttons, 2), nav_row(default=True)])


@dataclass(frozen=True, slots=True)
class PickerRow:
    """One picker line: a toggle for option ``index`` or, with ``index=None``, a header."""

    label: str
    index: int | None = None
    selected: bool = False


def picker_keyboard(
    rows: Sequence[PickerRow], page: int = 0, page_count: int = 1
) -> InlineKeyboardMarkup:
    """Checkbox list (leagues, teams, fixtures) with optional pagination ``◀️ 1/4 ▶️``."""
    lines: Rows = []
    for row in rows:
        if row.index is None:
            lines.append([_button(row.label, NoopCb())])
        else:
            label = _checked(row.label, on=row.selected)
            lines.append([_button(label, PickCb(action="toggle", index=row.index))])
    if page_count > 1:
        lines.append(
            [
                _button(texts.BTN_PREV_PAGE, PickCb(action="page", index=page - 1))
                if page > 0
                else _button(" ", NoopCb()),
                _button(texts.PAGE_INDICATOR.format(page=page + 1, pages=page_count), NoopCb()),
                _button(texts.BTN_NEXT_PAGE, PickCb(action="page", index=page + 1))
                if page + 1 < page_count
                else _button(" ", NoopCb()),
            ]
        )
    lines.append([_button(texts.BTN_DONE, PickCb(action="done"))])
    return _markup([*lines, nav_row()])


def count_keyboard(default: int, *, per_day: bool) -> InlineKeyboardMarkup:
    """Step 5: number of signals, "all", typed value and the per-day switch."""
    buttons = [
        _button(str(n) + (texts.DEFAULT_MARK if n == default else ""), CountCb(value=n))
        for n in WIZARD_COUNT_CHOICES
    ]
    buttons.append(_button(texts.BTN_ALL, CountCb(value=COUNT_ALL)))
    return _markup(
        [
            *_chunks(buttons, 3),
            [_button(texts.BTN_CUSTOM, CountCb(value=CUSTOM))],
            [
                _button(
                    texts.BTN_PER_DAY.format(state=texts.CHECKED if per_day else texts.UNCHECKED),
                    CountCb(value=COUNT_TOGGLE_PER_DAY),
                )
            ],
            nav_row(default=True),
        ]
    )


def markets_keyboard(
    selected: Iterable[MarketType], offered: Iterable[MarketType]
) -> InlineKeyboardMarkup:
    """Step 6: multi-select over the markets the chosen sports are priced in."""
    chosen, available = set(selected), set(offered)
    buttons = [
        _button(_checked(label, on=market in chosen), MarketCb(market=market.value))
        for market, label in texts.MARKET_LABELS.items()
        if market in available
    ]
    return _markup(
        [
            *_chunks(buttons, 2),
            [_button(texts.BTN_DONE, MarketCb(market="done"))],
            nav_row(default=True),
        ]
    )


def sort_keyboard(current: SortKey) -> InlineKeyboardMarkup:
    """Step 7: sort order."""
    rows = [
        [_button(_checked(label, on=key is current), SortCb(key=key.value))]
        for key, label in texts.SORT_LABELS.items()
    ]
    return _markup([*rows, nav_row(default=True)])


def confirm_keyboard(*, can_start: bool) -> InlineKeyboardMarkup:
    """Confirmation screen; "Boshlash" is hidden when the request is over budget."""
    first = [_button(texts.BTN_START, ConfirmCb(action="start"))] if can_start else []
    first.append(_button(texts.BTN_EDIT, ConfirmCb(action="edit")))
    return _markup(
        [
            first,
            [_button(texts.BTN_SAVE_TEMPLATE, ConfirmCb(action="save"))],
            nav_row(),
        ]
    )


EDIT_STEPS: Final = (
    ("dates", texts.BTN_EDIT_DATES),
    ("sport", texts.BTN_EDIT_SPORT),
    ("probability", texts.BTN_EDIT_PROBABILITY),
    ("scope", texts.BTN_EDIT_SCOPE),
    ("count", texts.BTN_EDIT_COUNT),
    ("markets", texts.BTN_EDIT_MARKETS),
    ("sort", texts.BTN_EDIT_SORT),
)


def edit_keyboard() -> InlineKeyboardMarkup:
    """Edit menu (✏️ O'zgartirish): jump to one step, then back to the confirmation."""
    buttons = [_button(label, EditCb(step=step)) for step, label in EDIT_STEPS]
    return _markup([*_chunks(buttons, 2), nav_row()])


# --------------------------------------------------------------------------- results
def card_keyboard(signal_id: int, *, retro: bool = False) -> InlineKeyboardMarkup:
    """Buttons under a signal card; a retro look-back cannot be recalculated or bet on."""
    if retro:
        return _markup(
            [
                [
                    _button(texts.BTN_DETAIL, CardCb(action="detail", signal_id=signal_id)),
                    _button(texts.BTN_HIDE, CardCb(action="hide", signal_id=signal_id)),
                ]
            ]
        )
    return _markup(
        [
            [
                _button(texts.BTN_RECALC, CardCb(action="recalc", signal_id=signal_id)),
                _button(texts.BTN_DETAIL, CardCb(action="detail", signal_id=signal_id)),
            ],
            [
                _button(texts.BTN_BET, CardCb(action="bet", signal_id=signal_id)),
                _button(texts.BTN_HIDE, CardCb(action="hide", signal_id=signal_id)),
            ],
        ]
    )


def more_results_keyboard(next_page: int, count: int) -> InlineKeyboardMarkup:
    """The ➡️ Yana N ta button under a result page."""
    return _markup([[_button(texts.BTN_MORE.format(count=count), ResultsCb(page=next_page))]])


# --------------------------------------------------------------------------- settings
def settings_keyboard() -> InlineKeyboardMarkup:
    """Overview: one button per editable field."""
    buttons = [
        _button(texts.SETTINGS_BUTTONS[name], SettingsCb(field=name)) for name in SETTINGS_FIELDS
    ]
    return _markup(_chunks(buttons, 2))


def _settings_footer(name: str) -> Rows:
    rows: Rows = []
    extra = []
    if name in TYPED_FIELDS:
        extra.append(_button(texts.BTN_CUSTOM, SettingsCb(field=name, action="input")))
    if name in OFF_FIELDS:
        extra.append(_button(texts.BTN_OFF, SettingsCb(field=name, action="off")))
    rows.append(extra)
    rows.append([_button(texts.BTN_SETTINGS_BACK, SettingsCb(field=name, action="back"))])
    return rows


def settings_choice_keyboard(
    name: str, labels: Sequence[str], *, current: int | None = None
) -> InlineKeyboardMarkup:
    """Preset choices of one field (``set`` with the preset index)."""
    buttons = [
        _button(
            _checked(label, on=index == current), SettingsCb(field=name, action="set", index=index)
        )
        for index, label in enumerate(labels)
    ]
    return _markup([*_chunks(buttons, 3), *_settings_footer(name)])


def settings_toggle_keyboard(
    name: str, labels: Sequence[str], selected: Sequence[bool]
) -> InlineKeyboardMarkup:
    """Multi-select field (sports, markets, leagues), saved on every toggle."""
    buttons = [
        _button(_checked(label, on=on), SettingsCb(field=name, action="toggle", index=index))
        for index, (label, on) in enumerate(zip(labels, selected, strict=True))
    ]
    return _markup([*_chunks(buttons, 2), *_settings_footer(name)])


def risk_keyboard(current: RiskProfile) -> InlineKeyboardMarkup:
    """The three presets of §6.5."""
    labels = [texts.RISK_LABELS[profile] for profile in SETTINGS_RISK_CHOICES]
    selected = SETTINGS_RISK_CHOICES.index(current) if current in SETTINGS_RISK_CHOICES else None
    return settings_choice_keyboard("risk", labels, current=selected)


def settings_input_keyboard(name: str) -> InlineKeyboardMarkup:
    """While a typed value is awaited: back to the field."""
    return _markup([[_button(texts.BTN_SETTINGS_BACK, SettingsCb(field=name, action="back"))]])
