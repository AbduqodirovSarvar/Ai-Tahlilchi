"""T6 fix round A: money has an upper bound everywhere it enters the system."""

from dataclasses import replace
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.bot.parsing import InputError, parse_money
from app.services.errors import ValidationFailedError
from app.services.interfaces import (
    MAX_MONEY,
    BetDraft,
    ResultMark,
    ResultMarkRequest,
    UserSettingsView,
)

JUST_OVER = MAX_MONEY + 1


def test_the_bound_is_one_trillion() -> None:
    assert Decimal(10) ** 12 == MAX_MONEY


@pytest.mark.parametrize("text", ["1000000000000", "1 000 000 000 000", "999_999_999_999,99"])
def test_parse_money_accepts_up_to_the_bound(text: str) -> None:
    assert 0 < parse_money(text) <= MAX_MONEY


@pytest.mark.parametrize("text", ["1000000000001", "1e15", "99999999999999999999", "inf", "nan"])
def test_parse_money_refuses_beyond_the_bound_and_non_numbers(text: str) -> None:
    with pytest.raises(InputError):
        parse_money(text)


def test_parse_money_zero_is_allowed_only_on_request() -> None:
    assert parse_money("0", allow_zero=True) == 0
    with pytest.raises(InputError):
        parse_money("0")


@given(st.decimals(min_value=Decimal("0.01"), max_value=MAX_MONEY, places=2, allow_nan=False))
def test_any_amount_within_the_bound_round_trips(amount: Decimal) -> None:
    assert parse_money(f"{amount}") == amount


@pytest.mark.parametrize("amount", [JUST_OVER, Decimal("1e30")])
def test_a_bet_draft_refuses_a_stake_beyond_the_bound(amount: Decimal) -> None:
    with pytest.raises(ValidationFailedError):
        BetDraft(signal_id="1", stake=amount)


def test_a_bet_draft_accepts_the_bound_itself() -> None:
    assert BetDraft(signal_id="1", stake=MAX_MONEY).stake == MAX_MONEY


@pytest.mark.parametrize("field", ["bank", "unit", "daily_loss_limit", "weekly_loss_limit"])
def test_settings_money_is_bounded(field: str) -> None:
    base = UserSettingsView()
    kwargs = {field: JUST_OVER}
    if field == "unit":  # a unit may not exceed the bank
        kwargs["bank"] = JUST_OVER
    with pytest.raises(ValidationFailedError):
        replace(base, **kwargs)


def test_settings_accept_money_at_the_bound() -> None:
    settings = replace(UserSettingsView(), bank=MAX_MONEY, unit=MAX_MONEY)
    assert settings.bank == MAX_MONEY


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cashout_profit_units": JUST_OVER},
        {"cashout_profit_units": -JUST_OVER},
        {"cashout_bet_id": "1", "cashout_payout": JUST_OVER},
    ],
)
def test_a_cash_out_amount_is_bounded(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationFailedError):
        ResultMarkRequest(signal_id="1", mark=ResultMark.CASHOUT, **kwargs)  # type: ignore[arg-type]
