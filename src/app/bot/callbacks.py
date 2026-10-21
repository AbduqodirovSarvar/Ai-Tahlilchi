"""Typed ``callback_data`` factories (Telegram limit: 64 bytes per button).

Prefixes are two letters and values are short enum codes or small integers. Catalog ids
(``EntityId``, up to 64 bytes and possibly containing ``:``) never go into a button: pickers
keep the options of the visible page in FSM data and the button carries the option index.
"""

from aiogram.filters.callback_data import CallbackData


class NavCb(CallbackData, prefix="nv"):
    """Wizard navigation: ``back``, ``cancel`` or ``default`` (take the settings value)."""

    action: str


class DateCb(CallbackData, prefix="wd"):
    """Date-step choice: ``today``, ``tomorrow``, ``weekend``, ``days`` or ``calendar``."""

    choice: str
    days: int = 0


class CalendarCb(CallbackData, prefix="wc"):
    """Calendar widget: ``month`` (value = year*100+month), ``day`` (ordinal) or ``noop``."""

    action: str
    value: int = 0


class SportCb(CallbackData, prefix="ws"):
    """Sport choice; ``all`` selects every sport."""

    sport: str


class ProbabilityCb(CallbackData, prefix="wp"):
    """Minimal probability in percent; ``-1`` asks for a typed value."""

    percent: int


class EvCb(CallbackData, prefix="we"):
    """Minimal EV in percent; ``-1`` asks for a typed value."""

    percent: int


class ScopeCb(CallbackData, prefix="wq"):
    """Scope kind (``all``, ``leagues``, ``teams``, ``fixtures``)."""

    kind: str


class PickCb(CallbackData, prefix="wk"):
    """Checkbox pickers: ``toggle`` (index into FSM options), ``page`` or ``done``."""

    action: str
    index: int = 0


class CountCb(CallbackData, prefix="wn"):
    """Signal count; ``0`` = all, ``-1`` = typed value, ``-2`` = toggle per-day."""

    value: int


class MarketCb(CallbackData, prefix="wm"):
    """Market multi-select: a ``MarketType`` value or ``done``."""

    market: str


class SortCb(CallbackData, prefix="wo"):
    """Sort key (a ``SortKey`` value)."""

    key: str


class ConfirmCb(CallbackData, prefix="wf"):
    """Confirmation screen: ``start``, ``edit`` or ``save``."""

    action: str


class EditCb(CallbackData, prefix="wx"):
    """Jump from the confirmation screen to one wizard step."""

    step: str


class CardCb(CallbackData, prefix="sc"):
    """Signal card buttons: ``recalc``, ``detail``, ``bet`` or ``hide``."""

    action: str
    signal_id: int


class ResultsCb(CallbackData, prefix="rp"):
    """Next page of an analysis result (``page`` is zero-based)."""

    page: int


class SettingsCb(CallbackData, prefix="st"):
    """Settings: ``open`` a field editor, ``set`` a preset (index), ``toggle``, ``input``."""

    field: str
    action: str = "open"
    index: int = 0


class MenuCb(CallbackData, prefix="mn"):
    """Inline shortcuts back to a menu section (``settings``)."""

    section: str


class NoopCb(CallbackData, prefix="nx"):
    """Display-only button (day headers, page indicator)."""
