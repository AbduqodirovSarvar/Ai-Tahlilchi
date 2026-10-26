"""A stand-in for ``Services.calls`` until the pipeline branch (T7-C) is merged.

The contract DTOs live in :mod:`app.bot.call_log_types` (a temporary seam with the exact names
and fields of the contract). MERGE: when ``app.services.interfaces`` has the real types, the
seam becomes re-exports and this file keeps working unchanged.
"""

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from app.bot.call_log_types import (
    CallLogDetail,
    CallLogEntry,
    CallLogPage,
    CallLogRequest,
    CallLogStat,
)
from app.domain import CallOutcome, Provider
from app.services.errors import NotAuthorizedError, NotFoundError
from app.services.interfaces import Services

AT = datetime(2026, 10, 21, 7, 41, 5, tzinfo=UTC)
"""12:41:05 in Tashkent."""


def make_entry(call_id: int = 1, **overrides: object) -> CallLogEntry:
    """A successful ``fixtures`` call of API-Football."""
    base = CallLogEntry(
        call_id=call_id,
        at=AT,
        provider=Provider.API_FOOTBALL,
        operation="fixtures",
        outcome=CallOutcome.OK,
        duration_ms=230,
        http_status=200,
        quota_cost=1,
        request_preview='{"date": "2026-10-21"}',
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def make_detail(call_id: int = 1, **overrides: object) -> CallLogDetail:
    """The detail of :func:`make_entry` with a short request and response."""
    base = CallLogDetail(
        call_id=call_id,
        at=AT,
        provider=Provider.API_FOOTBALL,
        operation="fixtures",
        outcome=CallOutcome.OK,
        duration_ms=230,
        http_status=200,
        quota_cost=1,
        request_preview='{"date": "2026-10-21"}',
        request='{"date": "2026-10-21"}',
        response='{"response": []}',
        error=None,
        size_bytes=2048,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def make_stat(operation: str = "fixtures", **overrides: object) -> CallLogStat:
    """Ten API-Football calls of one operation."""
    base = CallLogStat(
        provider=Provider.API_FOOTBALL,
        operation=operation,
        calls=10,
        errors=1,
        cached=2,
        blocked=0,
        avg_duration_ms=220,
        quota_cost=7,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


class StubCallLog:
    """In-memory call log with the filter and paging rules of the real service."""

    def __init__(
        self,
        details: Sequence[CallLogDetail] = (),
        stats: Sequence[CallLogStat] = (),
        *,
        admin: bool = True,
    ) -> None:
        self.details = {detail.call_id: detail for detail in details}
        self.stats = tuple(stats)
        self.admin = admin
        self.requests: list[CallLogRequest] = []

    def _check(self, user_id: int) -> None:
        if not self.admin:
            raise NotAuthorizedError(user_id, "read the call log")

    async def list_calls(self, user_id: int, request: CallLogRequest) -> CallLogPage:
        """Newest first, filtered, paged; a page past the end is empty."""
        self._check(user_id)
        self.requests.append(request)
        matches = [
            detail
            for detail in sorted(self.details.values(), key=lambda d: (d.at, d.call_id), reverse=True)
            if (request.provider is None or detail.provider is request.provider)
            and (request.outcome is None or detail.outcome is request.outcome)
        ]
        first = request.page * request.page_size
        return CallLogPage(
            items=tuple(
                CallLogEntry(
                    call_id=d.call_id,
                    at=d.at,
                    provider=d.provider,
                    operation=d.operation,
                    outcome=d.outcome,
                    duration_ms=d.duration_ms,
                    http_status=d.http_status,
                    quota_cost=d.quota_cost,
                    request_preview=d.request_preview,
                )
                for d in matches[first : first + request.page_size]
            ),
            page=request.page,
            page_count=-(-len(matches) // request.page_size),
            total=len(matches),
        )

    async def get_call(self, user_id: int, call_id: int) -> CallLogDetail:
        """One call in full."""
        self._check(user_id)
        if call_id not in self.details:
            raise NotFoundError("call", str(call_id))
        return self.details[call_id]

    async def call_stats(self, user_id: int, *, days: int = 1) -> Sequence[CallLogStat]:
        """The scripted statistics."""
        self._check(user_id)
        _ = days
        return self.stats


class WithCalls:
    """``Services`` plus a ``calls`` attribute (the real field arrives with the T7-C merge)."""

    def __init__(self, services: Services, calls: StubCallLog) -> None:
        self._services = services
        self.calls = calls

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        return getattr(self._services, name)
