"""Pipeline → user messages, in the pipeline's own terms.

The service-level :class:`~app.services.interfaces.Notifier` speaks in service DTOs, which the
pipeline may not import (layering). The pipeline therefore talks to :class:`Delivery`, and
the services layer adapts it to the notifier (mapping cards to ``SignalView`` and so on).
Idempotency, quiet hours and bookkeeping live in the pipeline jobs; a ``Delivery`` just
sends. A method returns an opaque message reference (or ``None`` when nothing was
delivered), which the pipeline stores in the ledger so a later update can reply to it.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from app.db.enums import RecalcKind
from app.db.repo import FixtureRecord
from app.pipeline.ledger import LedgerKey
from app.pipeline.recalc import RecalcResult
from app.pipeline.types import AnalysisOutcome, SignalCard


@dataclass(frozen=True, slots=True, kw_only=True)
class DailyDelivery:
    """The morning digest: the user's analysis of ``day`` (their local date)."""

    day: date
    outcome: AnalysisOutcome


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdateDelivery:
    """A scheduled recalculation of one signal."""

    kind: RecalcKind
    result: RecalcResult
    lineups_confirmed: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class CancelDelivery:
    """A signal whose match was postponed, cancelled or abandoned (``reason`` = the status)."""

    card: SignalCard
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class WatchDelivery:
    """A watchlist event (``lineups``, ``odds_move`` or ``result``) about a fixture."""

    event: str
    fixture: FixtureRecord
    detail: str | None = None
    card: SignalCard | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SettledDelivery:
    """A signal that was just settled."""

    card: SignalCard
    profit_units: Decimal


class Delivery(Protocol):
    """Sends the pipeline's messages; see the module docstring."""

    async def daily(self, user_id: int, message: DailyDelivery, key: LedgerKey) -> str | None:
        """Send the morning digest."""
        ...

    async def update(
        self, user_id: int, message: UpdateDelivery, key: LedgerKey, reply_to: str | None
    ) -> str | None:
        """Send a recalculation result."""
        ...

    async def cancelled(
        self, user_id: int, message: CancelDelivery, key: LedgerKey, reply_to: str | None
    ) -> str | None:
        """Send a cancellation."""
        ...

    async def watch_event(self, user_id: int, message: WatchDelivery, key: LedgerKey) -> str | None:
        """Send a watchlist event."""
        ...

    async def settled(self, user_id: int, message: SettledDelivery, key: LedgerKey) -> str | None:
        """Send a settlement."""
        ...

    async def alert_admin(self, component: str, message: str, at: datetime) -> None:
        """Report a system error to the administrators."""
        ...


class NullDelivery:
    """Delivers nothing (jobs run for their stored side effects only)."""

    async def daily(self, user_id: int, message: DailyDelivery, key: LedgerKey) -> str | None:  # noqa: ARG002
        """Drop the message."""
        return None

    async def update(
        self,
        user_id: int,  # noqa: ARG002
        message: UpdateDelivery,  # noqa: ARG002
        key: LedgerKey,  # noqa: ARG002
        reply_to: str | None,  # noqa: ARG002
    ) -> str | None:
        """Drop the message."""
        return None

    async def cancelled(
        self,
        user_id: int,  # noqa: ARG002
        message: CancelDelivery,  # noqa: ARG002
        key: LedgerKey,  # noqa: ARG002
        reply_to: str | None,  # noqa: ARG002
    ) -> str | None:
        """Drop the message."""
        return None

    async def watch_event(
        self,
        user_id: int,  # noqa: ARG002
        message: WatchDelivery,  # noqa: ARG002
        key: LedgerKey,  # noqa: ARG002
    ) -> str | None:
        """Drop the message."""
        return None

    async def settled(
        self,
        user_id: int,  # noqa: ARG002
        message: SettledDelivery,  # noqa: ARG002
        key: LedgerKey,  # noqa: ARG002
    ) -> str | None:
        """Drop the message."""
        return None

    async def alert_admin(self, component: str, message: str, at: datetime) -> None:
        """Drop the alert."""
