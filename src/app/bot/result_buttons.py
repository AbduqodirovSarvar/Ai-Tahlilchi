"""Mark buttons of a signal (plan 6.13 A, F8.1): the pure pieces shared by cards and the list.

Kept free of :mod:`app.bot.keyboards` so the card keyboard there can use it without a cycle.
"""

from datetime import datetime
from typing import Final

from aiogram.types import InlineKeyboardButton

from app.bot import texts
from app.bot.callbacks import ResultCb
from app.services.interfaces import ResultMark, SignalState, SignalView

MARK_CODES: Final[dict[str, ResultMark]] = {
    "w": ResultMark.WON,
    "l": ResultMark.LOST,
    "v": ResultMark.VOID,
}
CASHOUT_CODE: Final = "c"
ORIGIN_LIST: Final = "l"


def _button(text: str, callback: ResultCb) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback.pack())


def markable(signal: SignalView, now: datetime | None) -> bool:
    """True when the card should offer the mark buttons.

    A recommended, live (not retro, not cancelled) signal can be marked once its match has
    started; a card that already shows a result keeps the buttons as a correction (plan 6.13 A:
    manual marks beat the API result). Without a clock only a settled card qualifies.
    """
    if not signal.recommended or signal.retro or signal.state is SignalState.CANCELLED:
        return False
    if signal.result is not None:
        return True
    return now is not None and signal.kickoff <= now


def mark_row(signal_id: int, *, page: int = 0, origin: str = "") -> list[InlineKeyboardButton]:
    """``[✅ O'tdi] [❌ O'tmadi] [↩️ Void] [💸 Cashout]`` for one signal."""
    buttons = [
        _button(
            texts.BTN_RESULT_MARKS[code],
            ResultCb(action="mark", signal_id=signal_id, value=code, page=page, origin=origin),
        )
        for code in MARK_CODES
    ]
    buttons.append(
        _button(
            texts.BTN_RESULT_MARKS[CASHOUT_CODE],
            ResultCb(action="cash", signal_id=signal_id, page=page, origin=origin),
        )
    )
    return buttons


def note_button(signal_id: int, *, page: int = 0, origin: str = "") -> InlineKeyboardButton:
    """``💬 Izoh``."""
    return _button(
        texts.BTN_RESULT_NOTE,
        ResultCb(action="note", signal_id=signal_id, page=page, origin=origin),
    )


def card_result_rows(signal_id: int) -> list[list[InlineKeyboardButton]]:
    """The rows added under a card: the mark buttons (two rows) and the note button."""
    marks = mark_row(signal_id)
    return [marks[:2], marks[2:], [note_button(signal_id)]]
