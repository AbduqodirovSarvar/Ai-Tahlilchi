"""Betting markets and the selections that can be backed in them.

A :class:`Market` is a market type plus, for line markets, its line: "Total 2.5" and
"Total 3.5" are different markets. Lines are quoted in steps of 0.25 (quarter lines such as
Asian handicap -0.75 split the stake over the two neighbouring half lines).

Asian handicap lines are always expressed **from the home team's side**: ``line=-0.5``
means home -0.5 / away +0.5 (football-data.co.uk's ``AHh`` uses the same convention).
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Self

from app.domain._validation import require_finite
from app.domain.errors import DomainValidationError

LINE_STEP: Final = 0.25
_STEPS_PER_UNIT: Final = round(1 / LINE_STEP)
_LINE_TOLERANCE: Final = 1e-9


class MarketType(StrEnum):
    """Kind of market. Values are stable identifiers stored in the database."""

    MATCH_RESULT = "1x2"
    DOUBLE_CHANCE = "double_chance"
    TOTALS = "totals"
    BTTS = "btts"
    ASIAN_HANDICAP = "asian_handicap"


class Selection(StrEnum):
    """An outcome that can be backed. Which ones are valid depends on the market type."""

    HOME = "home"
    DRAW = "draw"
    AWAY = "away"
    HOME_OR_DRAW = "home_draw"
    HOME_OR_AWAY = "home_away"
    DRAW_OR_AWAY = "draw_away"
    OVER = "over"
    UNDER = "under"
    YES = "yes"
    NO = "no"


SELECTIONS_BY_MARKET: Final[dict[MarketType, tuple[Selection, ...]]] = {
    MarketType.MATCH_RESULT: (Selection.HOME, Selection.DRAW, Selection.AWAY),
    MarketType.DOUBLE_CHANCE: (
        Selection.HOME_OR_DRAW,
        Selection.HOME_OR_AWAY,
        Selection.DRAW_OR_AWAY,
    ),
    MarketType.TOTALS: (Selection.OVER, Selection.UNDER),
    MarketType.BTTS: (Selection.YES, Selection.NO),
    MarketType.ASIAN_HANDICAP: (Selection.HOME, Selection.AWAY),
}
LINE_MARKETS: Final = frozenset({MarketType.TOTALS, MarketType.ASIAN_HANDICAP})


def normalize_line(line: float) -> float:
    """Snap ``line`` to the 0.25 grid, rejecting values that are not on it.

    Snapping removes float noise (``2.4999999999`` from a CSV) so equal lines compare equal.
    """
    finite = require_finite(line, "line")
    steps = round(finite * _STEPS_PER_UNIT)
    if abs(finite * _STEPS_PER_UNIT - steps) > _LINE_TOLERANCE * max(1.0, abs(finite)):
        msg = f"line must be a multiple of {LINE_STEP} (got {line})"
        raise DomainValidationError(msg)
    return steps / _STEPS_PER_UNIT + 0.0  # `+ 0.0` turns -0.0 into 0.0


@dataclass(frozen=True, slots=True)
class Market:
    """A market type with its line (``None`` for markets without a line)."""

    type: MarketType
    line: float | None = None

    def __post_init__(self) -> None:
        """Require a valid line exactly for line markets; totals lines are positive."""
        if self.type not in LINE_MARKETS:
            if self.line is not None:
                msg = f"market {self.type} has no line (got {self.line})"
                raise DomainValidationError(msg)
            return
        if self.line is None:
            msg = f"market {self.type} requires a line"
            raise DomainValidationError(msg)
        line = normalize_line(self.line)
        if self.type is MarketType.TOTALS and line <= 0:
            msg = f"totals line must be positive (got {line})"
            raise DomainValidationError(msg)
        object.__setattr__(self, "line", line)

    @property
    def selections(self) -> tuple[Selection, ...]:
        """Selections that can be backed in this market."""
        return SELECTIONS_BY_MARKET[self.type]

    def require_selection(self, selection: Selection) -> Selection:
        """Return ``selection`` if it belongs to this market, else raise."""
        if selection not in self.selections:
            msg = f"selection {selection} is not valid for market {self}"
            raise DomainValidationError(msg)
        return selection

    def __str__(self) -> str:
        """Readable form for logs: ``totals 2.5`` or ``1x2``."""
        return self.type.value if self.line is None else f"{self.type.value} {self.line:g}"

    @classmethod
    def match_result(cls) -> Self:
        """1X2: home win, draw or away win."""
        return cls(MarketType.MATCH_RESULT)

    @classmethod
    def double_chance(cls) -> Self:
        """Double chance: 1X, 12 or X2."""
        return cls(MarketType.DOUBLE_CHANCE)

    @classmethod
    def totals(cls, line: float) -> Self:
        """Total goals over/under ``line`` (e.g. 2.5)."""
        return cls(MarketType.TOTALS, line)

    @classmethod
    def btts(cls) -> Self:
        """Both teams to score: yes or no."""
        return cls(MarketType.BTTS)

    @classmethod
    def asian_handicap(cls, home_line: float) -> Self:
        """Asian handicap with ``home_line`` applied to the home team (e.g. -0.75)."""
        return cls(MarketType.ASIAN_HANDICAP, home_line)
