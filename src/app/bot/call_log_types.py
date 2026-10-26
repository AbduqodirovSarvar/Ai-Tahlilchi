"""The provider call log contract as the 📡 So'rovlar screens use it (v1.11, T7-A/T7-C).

TEMPORARY SEAM: ``Services.calls`` and these DTOs are written by the pipeline-engineer on another
branch. They are declared here, name for name, so the bot compiles and is tested before that
branch lands. At merge this module becomes plain re-exports::

    from app.services.interfaces import (
        CallLogDetail,
        CallLogEntry,
        CallLogPage,
        CallLogRequest,
        CallLogService,
        CallLogStat,
    )

and :func:`call_log_of` returns ``services.calls`` without the ``type: ignore``.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.domain import CallOutcome, Provider
from app.services.interfaces import Services


@dataclass(frozen=True, slots=True, kw_only=True)
class CallLogEntry:
    """One logged provider call as the list shows it."""

    call_id: int
    at: datetime
    provider: Provider
    operation: str
    outcome: CallOutcome
    duration_ms: int
    http_status: int | None
    quota_cost: int
    request_preview: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CallLogRequest:
    """A filtered page of the call log (newest first)."""

    provider: Provider | None = None
    outcome: CallOutcome | None = None
    page: int = 0
    page_size: int = 10


@dataclass(frozen=True, slots=True, kw_only=True)
class CallLogPage:
    """A page of calls; ``page_count`` is 0 when nothing matched."""

    items: tuple[CallLogEntry, ...]
    page: int
    page_count: int
    total: int


@dataclass(frozen=True, slots=True, kw_only=True)
class CallLogDetail:
    """A call with its full (already scrubbed and clipped) request and response."""

    call_id: int
    at: datetime
    provider: Provider
    operation: str
    outcome: CallOutcome
    duration_ms: int
    http_status: int | None
    quota_cost: int
    request_preview: str
    request: str
    response: str | None
    error: str | None
    size_bytes: int | None


@dataclass(frozen=True, slots=True, kw_only=True)
class CallLogStat:
    """Calls of one provider operation over a period."""

    provider: Provider
    operation: str
    calls: int
    errors: int
    cached: int
    blocked: int
    avg_duration_ms: int | None
    quota_cost: int


class CallLogService(Protocol):
    """Administrator-only look at the provider call log."""

    async def list_calls(self, user_id: int, request: CallLogRequest) -> CallLogPage:
        """A page of calls, newest first."""
        ...

    async def get_call(self, user_id: int, call_id: int) -> CallLogDetail:
        """One call in full (``NotFoundError`` when it is gone)."""
        ...

    async def call_stats(self, user_id: int, *, days: int = 1) -> Sequence[CallLogStat]:
        """Calls per provider and operation over the last ``days`` days."""
        ...


def call_log_of(services: Services) -> CallLogService:
    """The call log service of ``services`` (merge: ``return services.calls``)."""
    return services.calls  # type: ignore[attr-defined,no-any-return]  # TEMPORARY: see module doc
