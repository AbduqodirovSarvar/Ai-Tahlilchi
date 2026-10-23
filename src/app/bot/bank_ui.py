"""Pure parts of 🎟 Mening stavkalarim (plan §6.9, F7.5): overview, lists, bet screen, keyboards.

Money is shown as the user typed it (spaces between thousands); profit and loss come from
the service's settled bets. The totals here only add up what the service already computed.
"""

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Final
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardMarkup

from app.bot import texts
from app.bot.account_formatters import bet_lines, format_bets
from app.bot.callbacks import BankCb, BetCb, SettingsCb
from app.bot.formatters import describe_selection, escape, money, odds, percent
from app.bot.keyboards import markup_of, single_button
from app.services.interfaces import BankView, BetView, ResultMark, UserSettingsView

MAX_SETTLED_LINES: Final = 15
MAX_BET_BUTTONS: Final = 10
MARK_CODES: Final[dict[str, ResultMark]] = {
    "w": ResultMark.WON,
    "l": ResultMark.LOST,
    "v": ResultMark.VOID,
}
CASHOUT_CODE: Final = "c"


def signed_money(value: Decimal) -> str:
    """``+12 000`` / ``-500`` / ``0``."""
    text = money(value)
    return f"+{text}" if value > 0 else text


def _limit_line(settings: UserSettingsView, bank: BankView) -> list[str]:
    if settings.daily_loss_limit is None and settings.weekly_loss_limit is None:
        return []
    daily = (
        texts.BANK_LIMIT_NOT_SET
        if settings.daily_loss_limit is None
        else money(settings.daily_loss_limit)
    )
    weekly = (
        texts.BANK_LIMIT_NOT_SET
        if settings.weekly_loss_limit is None
        else money(settings.weekly_loss_limit)
    )
    return [
        texts.BANK_LIMITS.format(
            today=money(bank.loss_today),
            daily=daily,
            week=money(bank.loss_this_week),
            weekly=weekly,
        )
    ]


def format_overview(
    bank: BankView,
    bets: Sequence[BetView],
    settings: UserSettingsView,
    zone: ZoneInfo,
) -> str:
    """Bank, unit, open bets, the settled tally with profit and the loss-limit state.

    ``bets`` are the user's most recent bets (the open ones are listed); the settled tally and
    profit come from the service, which counts every bet, not only these.
    """
    open_bets = tuple(bet for bet in bets if bet.outcome is None)
    totals = bank.totals
    lines = [
        format_bets(bank, open_bets, zone),
        "",
        texts.BANK_UNIT.format(unit=money(settings.unit))
        if settings.unit is not None
        else texts.BANK_UNIT_UNSET,
        texts.BANK_SETTLED.format(
            count=totals.settled,
            won=totals.won,
            lost=totals.lost,
            void=totals.void,
            cashout=totals.cashout,
        ),
    ]
    yield_part = (
        ""
        if totals.yield_rate is None
        else texts.BANK_YIELD.format(value=percent(totals.yield_rate, signed=True, decimals=1))
    )
    lines.append(texts.BANK_PL.format(profit=signed_money(totals.profit), yield_part=yield_part))
    lines.extend(_limit_line(settings, bank))
    return "\n".join(lines)


def overview_keyboard(open_count: int) -> InlineKeyboardMarkup:
    """Open and settled lists, the chart and the bank setup."""
    return markup_of(
        [
            [
                single_button(texts.BTN_BANK_OPEN.format(count=open_count), BankCb(action="open")),
                single_button(texts.BTN_BANK_SETTLED, BankCb(action="settled")),
            ],
            [
                single_button(texts.BTN_BANK_CHART, BankCb(action="chart")),
                single_button(texts.BTN_BANK_SETUP, BankCb(action="setup")),
            ],
        ]
    )


def _selection(bet: BetView) -> str:
    return describe_selection(bet.market, bet.selection, texts.BET_SIDE_HOME, texts.BET_SIDE_AWAY)


def format_open_list(bets: Sequence[BetView], zone: ZoneInfo) -> str:
    """The open bets (text lines come from the shared bets formatter)."""
    if not bets:
        return texts.BETS_EMPTY
    head = texts.BANK_OPEN_TITLE.format(count=len(bets))
    body = "\n".join(bet_lines(list(bets), zone))
    return f"{head}\n\n{body}"


def open_keyboard(bets: Sequence[BetView]) -> InlineKeyboardMarkup:
    """One button per open bet (to mark its result) and back."""
    rows = [
        [
            single_button(
                f"{bet.fixture_label} · {odds(bet.odds_taken)}",
                BankCb(action="bet", id=bet.bet_id),
            )
        ]
        for bet in bets[:MAX_BET_BUTTONS]
    ]
    rows.append([single_button(texts.BTN_BANK_HOME, BankCb(action="home"))])
    return markup_of(rows)


def format_settled_list(bets: Sequence[BetView]) -> str:
    """The latest settled bets with result and profit."""
    settled = [bet for bet in bets if bet.outcome is not None]
    if not settled:
        return texts.BANK_SETTLED_EMPTY
    shown = settled[:MAX_SETTLED_LINES]
    lines = [texts.BANK_SETTLED_TITLE.format(count=len(shown))]
    for bet in shown:
        outcome = bet.outcome
        assert outcome is not None  # noqa: S101 - filtered above
        profit = "" if bet.profit is None else signed_money(bet.profit)
        lines.append(
            texts.BANK_SETTLED_LINE.format(
                fixture=escape(bet.fixture_label),
                selection=_selection(bet),
                odds=odds(bet.odds_taken),
                stake=money(bet.stake),
                result=texts.RESULT_LABELS[outcome],
                profit=profit,
            )
        )
    return "\n".join(lines)


def back_keyboard() -> InlineKeyboardMarkup:
    """Only the way back to the overview."""
    return markup_of([[single_button(texts.BTN_BANK_HOME, BankCb(action="home"))]])


def format_bet(bet: BetView) -> str:
    """The screen of one open bet."""
    return texts.BANK_BET_TITLE.format(
        fixture=escape(bet.fixture_label),
        selection=_selection(bet),
        odds=odds(bet.odds_taken),
        stake=money(bet.stake),
    )


def bet_keyboard(bet_id: int) -> InlineKeyboardMarkup:
    """Won, lost, void, cash-out and back to the open list."""
    marks = [
        single_button(texts.RESULT_LABELS[mark], BankCb(action="mark", id=bet_id, value=code))
        for code, mark in MARK_CODES.items()
    ]
    marks.append(
        single_button(texts.BTN_MARK_CASHOUT, BankCb(action="mark", id=bet_id, value=CASHOUT_CODE))
    )
    return markup_of(
        [
            marks[:2],
            marks[2:],
            [single_button(texts.BTN_BANK_HOME, BankCb(action="open"))],
        ]
    )


def setup_keyboard() -> InlineKeyboardMarkup:
    """Bank, unit and the two loss limits (the settings editors) and back."""
    fields = ("bank", "unit", "daily_loss", "weekly_loss")
    buttons = [
        single_button(texts.SETTINGS_BUTTONS[name], SettingsCb(field=name)) for name in fields
    ]
    return markup_of(
        [
            buttons[:2],
            buttons[2:],
            [single_button(texts.BTN_BANK_HOME, BankCb(action="home"))],
        ]
    )


def limit_ask_keyboard(signal_id: int) -> InlineKeyboardMarkup:
    """Continue anyway or give up."""
    return markup_of(
        [
            [
                single_button(texts.BTN_BANK_FORCE, BetCb(action="force", signal_id=signal_id)),
                single_button(texts.BTN_BET_CANCEL, BetCb(action="cancel", signal_id=signal_id)),
            ]
        ]
    )


def chart_points(bank: BankView) -> list[tuple[date, Decimal]]:
    """The bank history as plain ``(day, balance)`` pairs for the chart."""
    return [(point.day, point.balance) for point in bank.history]
