"""Bookmaker prices observed at a point in time."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from app.domain._validation import require_finite, require_slug, require_utc
from app.domain.entities import ExternalRef
from app.domain.errors import DomainValidationError
from app.domain.markets import Market, Selection

MIN_DECIMAL_ODDS_EXCLUSIVE: Final = 1.0

# Well-known bookmaker identifiers. Any lower-case slug is accepted; these are the ones
# several layers refer to. Market average/maximum are aggregates across bookmakers (as
# published by football-data.co.uk), stored as pseudo-bookmakers.
BOOKMAKER_PINNACLE: Final = "pinnacle"
BOOKMAKER_BET365: Final = "bet365"
BOOKMAKER_MARKET_AVERAGE: Final = "market_avg"
BOOKMAKER_MARKET_MAXIMUM: Final = "market_max"


class OddsKind(StrEnum):
    """Why/when a price was captured; drives line-movement and CLV analysis."""

    OPENING = "opening"
    MORNING = "morning"
    RECALC = "recalc"
    PREMATCH = "prematch"
    CLOSING = "closing"


@dataclass(frozen=True, slots=True, kw_only=True)
class OddsSnapshot:
    """Decimal odds for one selection of one market of one fixture at ``taken_at`` (UTC)."""

    fixture: ExternalRef
    bookmaker: str
    market: Market
    selection: Selection
    odds: float
    taken_at: datetime
    kind: OddsKind

    def __post_init__(self) -> None:
        """Check the bookmaker id, the selection, the price and the timestamp."""
        require_slug(self.bookmaker, "bookmaker")
        self.market.require_selection(self.selection)
        odds = require_finite(self.odds, "odds")
        if odds <= MIN_DECIMAL_ODDS_EXCLUSIVE:
            msg = f"decimal odds must be > {MIN_DECIMAL_ODDS_EXCLUSIVE} (got {self.odds})"
            raise DomainValidationError(msg)
        object.__setattr__(self, "odds", odds)
        object.__setattr__(self, "taken_at", require_utc(self.taken_at, "taken_at"))

    @property
    def line(self) -> float | None:
        """The market line (``None`` for markets without one)."""
        return self.market.line
