"""An independent settlement oracle: plain arithmetic, no import of ``app.models``.

A bet is a list of *legs* (weight, result) with weights summing to one: a quarter Asian line is
two half stakes on the two neighbouring half/whole lines. A leg wins, is returned (push) or
loses; the profit of a stake at decimal odds is then
``stake * (wins * (odds - 1) - losses)`` with ``wins`` and ``losses`` the weights of the legs.
"""

from decimal import Decimal
from fractions import Fraction
from typing import Final

from app.domain import Market, MarketType, Selection

WIN: Final = "win"
PUSH: Final = "push"
LOSS: Final = "loss"
Leg = tuple[Fraction, str]


def _sign_result(margin: Fraction) -> str:
    """A leg on ``margin`` (goals the pick is ahead by, handicap included)."""
    if margin > 0:
        return WIN
    return PUSH if margin == 0 else LOSS


def _split(line: Fraction) -> list[tuple[Fraction, Fraction]]:
    """``[(weight, line)]``: a quarter line is two half stakes on its neighbours."""
    quarter = line * 4
    if quarter.denominator == 1 and int(quarter) % 2 == 1:
        return [(Fraction(1, 2), line - Fraction(1, 4)), (Fraction(1, 2), line + Fraction(1, 4))]
    return [(Fraction(1), line)]


def legs(market: Market, selection: Selection, home: int, away: int) -> list[Leg]:
    """The legs of ``selection`` in ``market`` for the final regular-time score."""
    kind = market.type
    diff, total = home - away, home + away
    if kind is MarketType.MATCH_RESULT:
        won = {
            Selection.HOME: diff > 0,
            Selection.DRAW: diff == 0,
            Selection.AWAY: diff < 0,
        }[selection]
        return [(Fraction(1), WIN if won else LOSS)]
    if kind is MarketType.DOUBLE_CHANCE:
        won = {
            Selection.HOME_OR_DRAW: diff >= 0,
            Selection.DRAW_OR_AWAY: diff <= 0,
            Selection.HOME_OR_AWAY: diff != 0,
        }[selection]
        return [(Fraction(1), WIN if won else LOSS)]
    if kind is MarketType.BTTS:
        both = home > 0 and away > 0
        return [(Fraction(1), WIN if both == (selection is Selection.YES) else LOSS)]
    assert market.line is not None
    line = Fraction(str(market.line))
    if kind is MarketType.TOTALS:
        sign = 1 if selection is Selection.OVER else -1
        return [(w, _sign_result(sign * (total - ln))) for w, ln in _split(line)]
    assert kind is MarketType.ASIAN_HANDICAP
    # the line is the HOME handicap; an away pick takes the opposite side of it
    sign = 1 if selection is Selection.HOME else -1
    return [(w, _sign_result(sign * (diff + ln))) for w, ln in _split(line)]


def factor(parts: list[Leg], odds: float) -> Fraction:
    """Profit per unit staked at ``odds``."""
    price = Fraction(str(odds)) - 1
    return sum((w * (price if r == WIN else -1 if r == LOSS else 0) for w, r in parts), Fraction(0))


def money(stake: Decimal, parts: list[Leg], odds: float) -> Decimal:
    """Exact profit of a money stake (``Decimal``), 4 decimal places."""
    value = Fraction(stake) * factor(parts, odds)
    return (Decimal(value.numerator) / Decimal(value.denominator)).quantize(Decimal("0.0001"))


def mark_of(parts: list[Leg]) -> str:
    """The ``ResultMark`` value (``win``, ``half_win``, ``void``, ``half_loss``, ``loss``)."""
    wins = sum((w for w, r in parts if r == WIN), Fraction(0))
    losses = sum((w for w, r in parts if r == LOSS), Fraction(0))
    if wins and not losses:
        return "win" if wins == 1 else "half_win"
    if losses and not wins:
        return "loss" if losses == 1 else "half_loss"
    if not wins and not losses:
        return "void"
    return "half_win" if wins > losses else "half_loss"
