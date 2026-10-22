"""Every ``callback_data`` factory of the F7 features stays within Telegram's 64 bytes.

Fields are filled with the longest values the bot ever puts in them: 32-bit ids and indexes,
8-hex ``crc32`` keys and the longest enum codes.
"""

import pytest
from aiogram.filters.callback_data import CallbackData
from hypothesis import given
from hypothesis import strategies as st

from app.bot.callbacks import (
    BankCb,
    BetCb,
    CardCb,
    CustomCb,
    ExpressCb,
    ReportCb,
    TplCb,
    WatchCb,
)
from app.services.interfaces import ReportGroup

TELEGRAM_LIMIT = 64
INT32 = st.integers(min_value=-(2**31), max_value=2**31 - 1)
HEX_KEY = st.text(alphabet="0123456789abcdef", min_size=8, max_size=8)
ACTION = st.sampled_from(
    ["open", "settled", "chart", "setup", "home", "bet", "mark", "delete_yes", "add_league"]
)


def size(callback: CallbackData) -> int:
    return len(callback.pack().encode())


@given(action=ACTION, index=INT32)
def test_custom_and_express(action: str, index: int) -> None:
    assert size(CustomCb(action=action, index=index)) <= TELEGRAM_LIMIT
    assert size(ExpressCb(action=action, index=index)) <= TELEGRAM_LIMIT


@given(
    action=ACTION,
    kind=st.sampled_from(["t", "l", "h", "a"]),
    key=HEX_KEY,
    flag=st.sampled_from(["l", "o", "r"]),
)
def test_watch(action: str, kind: str, key: str, flag: str) -> None:
    assert size(WatchCb(action=action, kind=kind, key=key, flag=flag)) <= TELEGRAM_LIMIT


@given(action=ACTION, template_id=INT32, value=INT32)
def test_template(action: str, template_id: int, value: int) -> None:
    assert size(TplCb(action=action, id=template_id, value=value)) <= TELEGRAM_LIMIT


@given(action=ACTION, bet_id=INT32, value=st.sampled_from(["w", "l", "v", "c"]))
def test_bank(action: str, bet_id: int, value: str) -> None:
    assert size(BankCb(action=action, id=bet_id, value=value)) <= TELEGRAM_LIMIT


@given(action=ACTION, period=st.sampled_from(["7", "30", "all", "c"]))
def test_report(action: str, period: str) -> None:
    for group in ["", *(item.value for item in ReportGroup)]:
        assert size(ReportCb(action=action, period=period, group=group)) <= TELEGRAM_LIMIT


@given(action=ACTION, signal_id=INT32)
def test_cards_and_bets(action: str, signal_id: int) -> None:
    assert size(CardCb(action=action, signal_id=signal_id)) <= TELEGRAM_LIMIT
    assert size(BetCb(action=action, signal_id=signal_id)) <= TELEGRAM_LIMIT


@pytest.mark.parametrize(
    "callback",
    [
        WatchCb(action="notify", kind="t", key="ffffffff", flag="o"),
        TplCb(action="schedule", id=2**31 - 1, value=7),
        BankCb(action="mark", id=2**31 - 1, value="c"),
        ReportCb(action="chart", period="all", group="risk_profile"),
    ],
)
def test_packed_data_round_trips(callback: CallbackData) -> None:
    assert type(callback).unpack(callback.pack()) == callback
