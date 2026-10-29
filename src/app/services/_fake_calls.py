"""In-memory :class:`~app.services.interfaces.CallLogService` with a sample call log.

Private to the fake backend (the bot's development and tests). The log is a fixed set of
calls laid out backwards from the first moment it is read: API-Football and Gemini, every
outcome, a few days of history. It mimics the real service's rules: administrators only,
newest first, a page past the end is empty and ``days`` is bounded.
"""

from collections import defaultdict
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final

from app.domain import CallOutcome, CallRecord, Provider
from app.services.errors import NotAuthorizedError, NotFoundError
from app.services.interfaces import (
    CallLogDetail,
    CallLogEntry,
    CallLogPage,
    CallLogRequest,
    CallLogStat,
    check_call_stats_days,
)

if TYPE_CHECKING:
    from app.services.fake import FakeState

PREVIEW_CHARS: Final = 200
_API: Final = Provider.API_FOOTBALL
_LLM: Final = Provider.GEMINI
_MATCHES: Final = Provider.FOOTBALL_DATA
_REACHED: Final = (CallOutcome.OK, CallOutcome.ERROR)
_MINUTES_PER_DAY: Final = 24 * 60

# Columns: age in minutes, provider, operation, outcome, duration, HTTP status, quota cost.
_SAMPLE: Final[tuple[tuple[int, Provider, str, CallOutcome, int, int | None, int], ...]] = (
    (3 * _MINUTES_PER_DAY + 30, _API, "fixtures", CallOutcome.OK, 410, 200, 1),
    (3 * _MINUTES_PER_DAY + 20, _LLM, "llm_review", CallOutcome.OK, 5200, None, 1),
    (300, _API, "fixtures", CallOutcome.OK, 380, 200, 1),
    (290, _API, "odds", CallOutcome.OK, 720, 200, 1),
    (280, _API, "odds", CallOutcome.OK, 690, 200, 1),
    (270, _API, "odds", CallOutcome.CACHED, 0, None, 0),
    (240, _LLM, "llm_review", CallOutcome.OK, 4800, None, 1),
    (200, _API, "results", CallOutcome.ERROR, 15000, 500, 1),
    (190, _API, "results", CallOutcome.OK, 450, 200, 1),
    (150, _MATCHES, "matches", CallOutcome.OK, 320, 200, 1),
    (120, _LLM, "post_mortem", CallOutcome.ERROR, 9000, None, 3),
    (90, _API, "fixtures", CallOutcome.QUOTA_BLOCKED, 0, None, 0),
    (60, _LLM, "llm_review", CallOutcome.OK, 6100, None, 1),
    (45, _MATCHES, "matches/497410", CallOutcome.CACHED, 0, None, 0),
    (30, _API, "odds", CallOutcome.CACHED, 0, None, 0),
    (10, _API, "fixtures", CallOutcome.OK, 400, 200, 1),
)


def sample_call_records(now: datetime) -> tuple[CallRecord, ...]:
    """The sample log, oldest first, laid out backwards from ``now``."""
    records: list[CallRecord] = []
    for age, provider, operation, outcome, duration, status, cost in _SAMPLE:
        has_body = outcome is CallOutcome.OK
        body = f'{{"operation": "{operation}", "provider": "{provider.value}"}}'
        records.append(
            CallRecord(
                at=now - timedelta(minutes=age),
                provider=provider,
                operation=operation,
                outcome=outcome,
                duration_ms=duration,
                request=f"{operation} {body}",
                response=body if has_body else None,
                http_status=status,
                quota_cost=cost,
                size_bytes=len(body) if has_body else None,
                error="upstream timeout" if outcome is CallOutcome.ERROR else None,
            )
        )
    return tuple(records)


def _entry(call_id: int, record: CallRecord) -> CallLogEntry:
    return CallLogEntry(
        call_id=call_id,
        at=record.at,
        provider=record.provider,
        operation=record.operation,
        outcome=record.outcome,
        duration_ms=record.duration_ms,
        http_status=record.http_status,
        quota_cost=record.quota_cost,
        request_preview=record.request[:PREVIEW_CHARS],
    )


def _detail(call_id: int, record: CallRecord) -> CallLogDetail:
    entry = _entry(call_id, record)
    return CallLogDetail(
        call_id=entry.call_id,
        at=entry.at,
        provider=entry.provider,
        operation=entry.operation,
        outcome=entry.outcome,
        duration_ms=entry.duration_ms,
        http_status=entry.http_status,
        quota_cost=entry.quota_cost,
        request_preview=entry.request_preview,
        request=record.request,
        response=record.response,
        error=record.error,
        size_bytes=record.size_bytes,
    )


def _stat(key: tuple[Provider, str], records: list[CallRecord]) -> CallLogStat:
    reached = [r.duration_ms for r in records if r.outcome in _REACHED]
    return CallLogStat(
        provider=key[0],
        operation=key[1],
        calls=len(records),
        errors=sum(1 for r in records if r.outcome is CallOutcome.ERROR),
        cached=sum(1 for r in records if r.outcome is CallOutcome.CACHED),
        blocked=sum(1 for r in records if r.outcome is CallOutcome.QUOTA_BLOCKED),
        avg_duration_ms=round(sum(reached) / len(reached)) if reached else None,
        quota_cost=sum(r.quota_cost for r in records),
    )


class FakeCallLogService:
    """Sample-log implementation of the call log."""

    def __init__(self, state: "FakeState", *, admin_ids: frozenset[int] | None = None) -> None:
        """Read the log through ``state``'s clock; ``admin_ids=None`` lets everyone in."""
        self._state = state
        self._admin_ids = admin_ids
        self._log: tuple[tuple[int, CallRecord], ...] | None = None

    def _require_admin(self, user_id: int) -> None:
        if self._admin_ids is not None and user_id not in self._admin_ids:
            raise NotAuthorizedError(user_id, "read the call log")

    def _calls(self) -> tuple[tuple[int, CallRecord], ...]:
        """Numbered calls, newest first; laid out once so the ids stay stable."""
        if self._log is None:
            numbered = enumerate(sample_call_records(self._state.now()), start=1)
            self._log = tuple(reversed(tuple(numbered)))
        return self._log

    async def list_calls(self, user_id: int, request: CallLogRequest) -> CallLogPage:
        """See :meth:`CallLogService.list_calls`."""
        self._require_admin(user_id)
        matching = [
            (call_id, record)
            for call_id, record in self._calls()
            if (request.provider is None or record.provider is request.provider)
            and (request.outcome is None or record.outcome is request.outcome)
        ]
        start = request.page * request.page_size
        window = matching[start : start + request.page_size]
        return CallLogPage(
            items=tuple(_entry(call_id, record) for call_id, record in window),
            page=request.page,
            page_count=-(-len(matching) // request.page_size),
            total=len(matching),
        )

    async def get_call(self, user_id: int, call_id: int) -> CallLogDetail:
        """See :meth:`CallLogService.get_call`."""
        self._require_admin(user_id)
        for number, record in self._calls():
            if number == call_id:
                return _detail(number, record)
        raise NotFoundError("call", call_id)

    async def call_stats(self, user_id: int, *, days: int = 1) -> tuple[CallLogStat, ...]:
        """See :meth:`CallLogService.call_stats`."""
        self._require_admin(user_id)
        check_call_stats_days(days)
        since = self._state.now() - timedelta(days=days)
        groups: defaultdict[tuple[Provider, str], list[CallRecord]] = defaultdict(list)
        for _, record in self._calls():
            if record.at >= since:
                groups[(record.provider, record.operation)].append(record)
        return tuple(_stat(key, groups[key]) for key in sorted(groups))
