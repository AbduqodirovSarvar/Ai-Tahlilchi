"""Pure parts of 🧩 Ekspress tuzuvchi (plan §6.11, F7.8): picker draft, screens and result.

The service owns the rules and the arithmetic (``ExpressService``); the picker only guards the
obvious mistakes early (two picks of one match, more than four legs) so the user gets an
instant answer instead of an error after pressing "Hisoblash".
"""

from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any, Final, Self
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardMarkup

from app.bot import texts
from app.bot.callbacks import ExpressCb
from app.bot.draft import StaleDraftError
from app.bot.formatters import (
    clock,
    describe_selection,
    escape,
    odds,
    percent,
    short_date,
)
from app.bot.keyboards import checked_label, markup_of, single_button
from app.services.interfaces import (
    MAX_EXPRESS_LEGS,
    MIN_EXPRESS_LEGS,
    ExpressView,
    SignalState,
    SignalView,
)

MAX_PICKABLE: Final = 12
"""Signals offered in the picker (a keyboard of this size stays readable)."""


@dataclass(frozen=True, slots=True)
class ExpressOption:
    """One signal of the picker; buttons carry its index."""

    signal_id: int
    label: str
    fixture_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ExpressDraft:
    """The picker's state, JSON-safe for FSM storage."""

    message_id: int | None = None
    options: tuple[ExpressOption, ...] = ()
    selected: tuple[int, ...] = ()

    def to_data(self) -> dict[str, Any]:
        """JSON-safe form."""
        data = asdict(self)
        data["options"] = [list(asdict(option).values()) for option in self.options]
        return data

    @classmethod
    def from_data(cls, data: object) -> Self:
        """Decode, or raise :class:`StaleDraftError` when missing or corrupt."""
        if not isinstance(data, dict):
            raise StaleDraftError
        try:
            options = tuple(ExpressOption(int(a), str(b), str(c)) for a, b, c in data["options"])
            draft = cls(
                message_id=data["message_id"],
                options=options,
                selected=tuple(int(item) for item in data["selected"]),
            )
        except (KeyError, TypeError, ValueError):
            raise StaleDraftError from None
        known = {option.signal_id for option in options}
        if not set(draft.selected) <= known:
            raise StaleDraftError
        return draft

    def option(self, index: int) -> ExpressOption:
        """Option ``index``.

        Raises:
            StaleDraftError: no such option (a button of an older list).
        """
        if not 0 <= index < len(self.options):
            raise StaleDraftError
        return self.options[index]

    def selected_options(self) -> list[ExpressOption]:
        """The chosen options in list order."""
        chosen = set(self.selected)
        return [option for option in self.options if option.signal_id in chosen]

    def toggled(self, index: int) -> Self:
        """Switch option ``index``; the guards are checked by :meth:`refusal` first."""
        signal_id = self.option(index).signal_id
        if signal_id in self.selected:
            return replace(self, selected=tuple(i for i in self.selected if i != signal_id))
        return replace(self, selected=(*self.selected, signal_id))

    def refusal(self, index: int) -> str | None:
        """Why selecting option ``index`` is not allowed (an alert text), or ``None``."""
        option = self.option(index)
        if option.signal_id in self.selected:
            return None
        if len(self.selected) >= MAX_EXPRESS_LEGS:
            return texts.EXPRESS_LIMIT.format(max=MAX_EXPRESS_LEGS)
        if any(item.fixture_id == option.fixture_id for item in self.selected_options()):
            return texts.EXPRESS_SAME_MATCH
        return None


def pickable(signals: Sequence[SignalView], now: datetime) -> list[SignalView]:
    """Signals worth offering: recommended, active, unsettled, not started, not retro."""
    return [
        signal
        for signal in signals
        if signal.recommended
        and not signal.retro
        and signal.state is SignalState.ACTIVE
        and signal.result is None
        and signal.kickoff > now
    ][:MAX_PICKABLE]


def option_of(signal: SignalView, stamp: str) -> ExpressOption:
    """The picker line of ``signal``: ``22.10 20:00 Arsenal — Chelsea · @1.85``."""
    return ExpressOption(
        signal_id=signal.signal_id,
        label=f"{stamp} {signal.fixture_label} · @{odds(signal.odds)}",
        fixture_id=signal.fixture_id,
    )


def local_stamp(signal: SignalView, zone: ZoneInfo) -> str:
    """``22.10 20:00`` in the user's zone."""
    local = signal.kickoff.astimezone(zone)
    return f"{short_date(local.date())} {clock(local.time())}"


def pick_text(draft: ExpressDraft) -> str:
    """The picker's heading."""
    return texts.EXPRESS_PICK.format(count=len(draft.selected))


def pick_keyboard(draft: ExpressDraft) -> InlineKeyboardMarkup:
    """One toggle per signal, the calculate button once 2-4 are chosen, and close."""
    chosen = set(draft.selected)
    rows = [
        [
            single_button(
                checked_label(option.label, on=option.signal_id in chosen),
                ExpressCb(action="toggle", index=index),
            )
        ]
        for index, option in enumerate(draft.options)
    ]
    if MIN_EXPRESS_LEGS <= len(draft.selected) <= MAX_EXPRESS_LEGS:
        label = texts.BTN_EXPRESS_CALC.format(count=len(draft.selected))
        rows.append([single_button(label, ExpressCb(action="calc"))])
    rows.append([single_button(texts.BTN_EXPRESS_CLOSE, ExpressCb(action="close"))])
    return markup_of(rows)


def format_express(view: ExpressView) -> str:
    """Legs, combined numbers, the express's own weakness and the standing warning."""
    lines = [texts.EXPRESS_RESULT_HEADER.format(count=len(view.legs))]
    for number, leg in enumerate(view.legs, start=1):
        lines.append(
            texts.EXPRESS_LEG.format(
                number=number,
                fixture=escape(leg.fixture_label),
                selection=describe_selection(
                    leg.market, leg.selection, texts.BET_SIDE_HOME, texts.BET_SIDE_AWAY
                ),
                odds=odds(leg.odds),
                probability=percent(leg.probability),
            )
        )
    lines.append("")
    lines.append(
        texts.EXPRESS_TOTALS.format(
            odds=odds(view.combined_odds),
            probability=percent(view.combined_probability),
            fair_odds=odds(view.fair_odds),
            ev=percent(view.ev, signed=True, decimals=1),
        )
    )
    if view.ev < 0:
        lines.append(texts.EXPRESS_NEGATIVE)
    probabilities = [leg.probability for leg in view.legs]
    lines.append(
        texts.EXPRESS_YOURS.format(
            count=len(view.legs),
            low=percent(min(probabilities)),
            high=percent(max(probabilities)),
            probability=percent(view.combined_probability),
        )
    )
    lines.append(texts.EXPRESS_WARNING)
    return "\n".join(lines)


def result_keyboard() -> InlineKeyboardMarkup:
    """Back to the picker, or close."""
    return markup_of(
        [
            [
                single_button(texts.BTN_EXPRESS_BACK, ExpressCb(action="back")),
                single_button(texts.BTN_EXPRESS_CLOSE, ExpressCb(action="close")),
            ]
        ]
    )
