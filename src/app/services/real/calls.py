"""Real :class:`~app.services.interfaces.CallLogService`: the provider call log (v1.12).

A plain read of ``provider_calls`` for administrators. The list never loads response text;
only :meth:`RealCallLogService.get_call` does.
"""

from datetime import timedelta

from app.pipeline import data
from app.services.errors import NotAuthorizedError, NotFoundError
from app.services.interfaces import (
    CallLogDetail,
    CallLogEntry,
    CallLogPage,
    CallLogRequest,
    CallLogStat,
    check_call_stats_days,
)
from app.services.real.core import Core

PREVIEW_CHARS = 200
"""Request preview length, same as the list query's ``substr``."""


def _entry(row: data.CallSummary) -> CallLogEntry:
    return CallLogEntry(
        call_id=row.id,
        at=row.at,
        provider=row.provider,
        operation=row.operation,
        outcome=row.outcome,
        duration_ms=row.duration_ms,
        http_status=row.http_status,
        quota_cost=row.quota_cost,
        request_preview=row.request_preview,
    )


class RealCallLogService:
    """:class:`~app.services.interfaces.CallLogService` over the database."""

    def __init__(self, core: Core) -> None:
        """Share ``core`` with the other real services."""
        self._core = core

    def _require_admin(self, user_id: int, action: str) -> None:
        if not self._core.is_admin(user_id):
            raise NotAuthorizedError(user_id, action)

    async def list_calls(self, user_id: int, request: CallLogRequest) -> CallLogPage:
        """See :meth:`CallLogService.list_calls`."""
        self._require_admin(user_id, "read the call log")
        async with self._core.ctx.sessions() as session:
            total = await data.count_calls(
                session, provider=request.provider, outcome=request.outcome
            )
            rows = await data.list_calls(
                session,
                provider=request.provider,
                outcome=request.outcome,
                limit=request.page_size,
                offset=request.page * request.page_size,
            )
        return CallLogPage(
            items=tuple(_entry(row) for row in rows),
            page=request.page,
            page_count=-(-total // request.page_size),
            total=total,
        )

    async def get_call(self, user_id: int, call_id: int) -> CallLogDetail:
        """See :meth:`CallLogService.get_call`."""
        self._require_admin(user_id, "read the call log")
        async with self._core.ctx.sessions() as session:
            row = await data.get_call(session, call_id)
        if row is None:
            raise NotFoundError("call", call_id)
        return CallLogDetail(
            call_id=row.id,
            at=row.at,
            provider=row.provider,
            operation=row.operation,
            outcome=row.outcome,
            duration_ms=row.duration_ms,
            http_status=row.http_status,
            quota_cost=row.quota_cost,
            request_preview=row.request[:PREVIEW_CHARS],
            request=row.request,
            response=row.response,
            error=row.error,
            size_bytes=row.size_bytes,
        )

    async def call_stats(self, user_id: int, *, days: int = 1) -> tuple[CallLogStat, ...]:
        """See :meth:`CallLogService.call_stats`."""
        self._require_admin(user_id, "read the call log")
        check_call_stats_days(days)
        since = self._core.now() - timedelta(days=days)
        async with self._core.ctx.sessions() as session:
            stats = await data.call_stats(session, since)
        return tuple(
            CallLogStat(
                provider=stat.provider,
                operation=stat.operation,
                calls=stat.calls,
                errors=stat.errors,
                cached=stat.cached,
                blocked=stat.blocked,
                avg_duration_ms=None
                if stat.avg_duration_ms is None
                else round(stat.avg_duration_ms),
                quota_cost=stat.quota_cost,
            )
            for stat in stats
        )
