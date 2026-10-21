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
    """Next page of an analysis result (``page`` is zero-based).

    ``token`` names the result the button belongs to (see ``StoredResults``).
    """

    page: int
    token: int = 0


class SettingsCb(CallbackData, prefix="st"):
    """Settings: ``open`` a field editor, ``set`` a preset (index), ``toggle``, ``input``.

    ``toggle`` names its item by ``key`` (enum value, or a short hash of a league id with its
    ``index``) instead of a bare position, so a list that changed since the keyboard was
    drawn can never toggle a different item.
    """

    field: str
    action: str = "open"
    index: int = 0
    key: str = ""


class NoopCb(CallbackData, prefix="nx"):
    """Display-only button (day headers, page indicator)."""


WIZARD_CALLBACKS: tuple[type[CallbackData], ...] = (
    NavCb,
    DateCb,
    CalendarCb,
    SportCb,
    ProbabilityCb,
    EvCb,
    ScopeCb,
    PickCb,
    CountCb,
    MarketCb,
    SortCb,
    ConfirmCb,
    EditCb,
)
"""Every button of the 📊 Tahlil wizard (not cards, result paging or settings)."""
WIZARD_CALLBACK_PREFIXES: tuple[str, ...] = tuple(
    f"{callback.__prefix__}{callback.__separator__}" for callback in WIZARD_CALLBACKS
)
