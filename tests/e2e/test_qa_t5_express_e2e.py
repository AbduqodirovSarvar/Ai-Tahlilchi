"""QA (T5 bot): the express builder against an independent hand computation.

The oracle is plain arithmetic done here (``Decimal`` for the odds product, exact fractions for
the probability), never the production helper. The bot is driven through the real dispatcher
over the REAL services for the user-facing numbers and refusals.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.bot import texts
from app.bot.callbacks import ExpressCb
from app.services.errors import ValidationFailedError
from app.services.express_math import evaluate
from app.services.interfaces import MAX_EXPRESS_LEGS, MIN_EXPRESS_LEGS, DateSpec, SignalView
from tests.bot.samples import make_signal
from tests.e2e.bot_client import Client, answers, shown
from tests.e2e.real_world import ADMIN, RealBot
from tests.services.contract_suite import analyze_week

NOW = datetime(2026, 10, 22, 6, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=6)


def _leg(number: int, probability: float, odds: float, **extra: object) -> SignalView:
    return make_signal(
        signal_id=number,
        fixture_id=f"fx{number}",
        kickoff=LATER,
        probability=probability,
        odds=odds,
        ev=probability * odds - 1,
        **extra,
    )


# --------------------------------------------------------------------------- the arithmetic
def test_three_seventy_percent_legs_are_about_thirty_four_percent() -> None:
    legs = [_leg(1, 0.7, 1.5), _leg(2, 0.7, 1.6), _leg(3, 0.7, 1.7)]

    express = evaluate(legs, NOW)

    assert express.combined_probability == pytest.approx(0.343)  # 0.7 ** 3, by hand
    odds = Decimal("1.5") * Decimal("1.6") * Decimal("1.7")  # 4.08, exact
    assert Decimal(str(express.combined_odds)).quantize(Decimal("0.000001")) == odds
    assert express.ev == pytest.approx(float(Decimal("0.343") * odds - 1))  # +39.944%
    assert express.fair_odds == pytest.approx(1 / 0.343)  # 2.9155
    # The warning the plan asks for: three "70%" picks are not a 70% bet.
    assert round(express.combined_probability * 100) == 34


def test_odds_product_has_no_float_drift_worth_showing() -> None:
    legs = [_leg(i, 0.9, 1.1) for i in range(1, 5)]  # 1.1 ** 4 = 1.4641

    express = evaluate(legs, NOW)

    assert f"{express.combined_odds:.2f}" == "1.46"
    assert abs(Decimal(str(express.combined_odds)) - Decimal("1.4641")) < Decimal("1e-12")


def test_a_quarter_line_leg_contributes_its_own_ev() -> None:
    quarter = make_signal(
        signal_id=1,
        fixture_id="fx1",
        kickoff=LATER,
        probability=0.5,
        odds=2.0,
        ev=-0.10,  # half of the losses are half-stakes: not p * odds - 1 = 0
        zero_ev_odds=2.1,
    )
    plain = _leg(2, 0.5, 2.0)  # ev = 0

    express = evaluate([quarter, plain], NOW)

    assert express.ev == pytest.approx((1 - 0.10) * (1 + 0) - 1)


@given(
    legs=st.lists(
        st.tuples(st.floats(0.05, 0.99), st.floats(1.05, 6.0)),
        min_size=MIN_EXPRESS_LEGS,
        max_size=MAX_EXPRESS_LEGS,
    )
)
def test_express_invariants_against_an_exact_oracle(legs: list[tuple[float, float]]) -> None:
    views = [_leg(i + 1, p, o) for i, (p, o) in enumerate(legs)]

    express = evaluate(views, NOW)

    exact_p = Fraction(1)
    exact_o = Fraction(1)
    for p, o in legs:
        exact_p *= Fraction(p)
        exact_o *= Fraction(o)
    assert express.combined_probability == pytest.approx(float(exact_p), rel=1e-12)
    assert express.combined_odds == pytest.approx(float(exact_o), rel=1e-12)
    assert express.ev == pytest.approx(float(exact_p * exact_o - 1), rel=1e-9, abs=1e-12)
    assert express.combined_probability <= min(p for p, _ in legs) + 1e-15
    reordered = evaluate(list(reversed(views)), NOW)
    assert reordered.combined_probability == pytest.approx(express.combined_probability)
    assert reordered.ev == pytest.approx(express.ev)


def test_leg_count_boundaries_and_unplayable_legs() -> None:
    legs = [_leg(i, 0.6, 1.8) for i in range(1, 6)]
    for count in range(0, 7):
        views = legs[:count] if count <= 5 else [*legs, _leg(6, 0.6, 1.8)]
        if MIN_EXPRESS_LEGS <= count <= MAX_EXPRESS_LEGS:
            assert len(evaluate(views, NOW).legs) == count
        else:
            with pytest.raises(ValidationFailedError):
                evaluate(views, NOW)
    with pytest.raises(ValidationFailedError, match="correlated"):
        evaluate(
            [_leg(1, 0.6, 1.8), make_signal(signal_id=9, fixture_id="fx1", kickoff=LATER)], NOW
        )
    started = make_signal(signal_id=8, fixture_id="fx8", kickoff=NOW - timedelta(minutes=1))
    with pytest.raises(ValidationFailedError):
        evaluate([_leg(1, 0.6, 1.8), started], NOW)
    with pytest.raises(ValidationFailedError, match="same signal"):
        evaluate([_leg(1, 0.6, 1.8), _leg(1, 0.6, 1.8)], NOW)


# --------------------------------------------------------------------------- through the bot
async def _picker(
    rb: RealBot, *, every_market: bool = False
) -> tuple[Client, int, list[tuple[int, str]]]:
    """Open /ekspress; returns the client, the picker message and ``(signal_id, fixture_id)``
    of each option in button order (read from the stored draft, i.e. what the buttons mean)."""
    # the picker lists stored signals: run an analysis first
    await analyze_week(
        rb.services, ADMIN, dates=DateSpec.next_days(3), include_no_value=every_market
    )
    admin = rb.client()
    await admin.send("/ekspress")
    data = await rb.dispatcher.storage.get_data(admin.storage_key())
    options = [(int(row[0]), str(row[2])) for row in data["express"]["options"]]
    return admin, admin.last_message_id, options


def _one_per_match(options: list[tuple[int, str]]) -> list[int]:
    """Button indexes of the first option of each distinct match."""
    seen: set[str] = set()
    indexes = []
    for index, (_, fixture) in enumerate(options):
        if fixture not in seen:
            seen.add(fixture)
            indexes.append(index)
    return indexes


@pytest.mark.slow
async def test_bot_shows_the_hand_computed_numbers_for_three_matches(real_bot: RealBot) -> None:
    rb = real_bot
    admin, picker, options = await _picker(rb)
    indexes = _one_per_match(options)[:3]
    assert len(indexes) == 3
    for index in indexes:
        await admin.press_data(ExpressCb(action="toggle", index=index).pack(), picker)

    methods = await admin.press_data(ExpressCb(action="calc").pack(), picker)
    text = "\n".join(shown(methods))

    views = [await rb.services.signals.get_signal(ADMIN, options[i][0]) for i in indexes]
    p, o = Fraction(1), Decimal(1)
    for view in views:
        p *= Fraction(view.probability)
        o *= Decimal(str(view.odds))
    ev = float(p) * float(o) - 1
    assert f"{float(p) * 100:.0f}%" in text
    assert f"{o:.2f}" in text
    assert f"{ev * 100:+.1f}%" in text
    assert texts.EXPRESS_WARNING in text


@pytest.mark.slow
async def test_bot_refuses_a_second_pick_of_one_match_and_a_fifth_leg(real_bot: RealBot) -> None:
    rb = real_bot
    admin, picker, options = await _picker(rb, every_market=True)
    first_match = options[0][1]
    twins = [i for i, (_, fixture) in enumerate(options) if fixture == first_match]
    assert len(twins) >= 2, "the seeded world prices several markets per match"

    await admin.press_data(ExpressCb(action="toggle", index=twins[0]).pack(), picker)
    same_match = await admin.press_data(ExpressCb(action="toggle", index=twins[1]).pack(), picker)
    assert answers(same_match)[0].text == texts.EXPRESS_SAME_MATCH

    # the service is the final judge even when the bot's early guard is bypassed
    with pytest.raises(ValidationFailedError, match="correlated"):
        await rb.services.express.evaluate(ADMIN, [options[twins[0]][0], options[twins[1]][0]])

    others = [i for i in _one_per_match(options) if i != twins[0]]
    for index in others[: MAX_EXPRESS_LEGS - 1]:
        await admin.press_data(ExpressCb(action="toggle", index=index).pack(), picker)
    if len(others) >= MAX_EXPRESS_LEGS:  # a fifth different match
        fifth = await admin.press_data(
            ExpressCb(action="toggle", index=others[MAX_EXPRESS_LEGS - 1]).pack(), picker
        )
        assert answers(fifth)[0].text == texts.EXPRESS_LIMIT.format(max=MAX_EXPRESS_LEGS)
    calc = await admin.press_data(ExpressCb(action="calc").pack(), picker)
    assert "Ekspress" in "\n".join(shown(calc))


@pytest.mark.slow
async def test_express_calculate_needs_two_picks(real_bot: RealBot) -> None:
    rb = real_bot
    admin, picker, options = await _picker(rb)

    none = await admin.press_data(ExpressCb(action="calc").pack(), picker)
    await admin.press_data(ExpressCb(action="toggle", index=0).pack(), picker)
    one = await admin.press_data(ExpressCb(action="calc").pack(), picker)

    for methods in (none, one):
        assert answers(methods)[0].show_alert
        assert answers(methods)[0].text == texts.EXPRESS_NEED_TWO.format(min=MIN_EXPRESS_LEGS)
    assert options


@pytest.mark.slow
async def test_express_buttons_of_another_message_or_user_are_stale(real_bot: RealBot) -> None:
    rb = real_bot
    admin, picker, _ = await _picker(rb)
    toggle = ExpressCb(action="toggle", index=0).pack()

    wrong_message = await admin.press_data(toggle, picker + 500)
    other_user = await rb.client(1002).press_data(toggle, picker)

    for methods in (wrong_message, other_user):
        assert answers(methods)[0].show_alert
    assert await rb.services.bets.list_bets(ADMIN) == ()
