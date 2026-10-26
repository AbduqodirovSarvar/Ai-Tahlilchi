"""The database implementation of :class:`~app.domain.CallRecorder`.

Failure policy (ADR 0020): the call log is diagnostics, so a failure to write it must never
fail, slow down or alter the provider call it describes. :meth:`SqlCallRecorder.record`
therefore catches the storage and data errors a write can raise, logs the *type* and the call's
identity (never the exception text: SQLAlchemy errors embed the bound parameters, i.e. the
very prompt or body being stored) and returns. Cancellation is not swallowed.

Secrets: every text passes through the injected ``scrub`` function before it is cut and stored.
The function is injected because the scrubber (configured secret values and credential
patterns) lives in the pipeline layer, above this one.
"""

from collections.abc import Callable
from dataclasses import replace
from typing import Final

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.repo.calls import record_call
from app.domain import CallRecord, clip_text
from app.errors import DataError
from app.logging import get_logger

logger = get_logger(__name__)

MAX_LOGGED_ERROR: Final = 1_000
"""Longest error text kept per call (errors are short; a long one is a body echoed back)."""

type Scrub = Callable[[str], str]


class SqlCallRecorder:
    """Stores each reported call in ``provider_calls`` in its own short transaction."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], *, scrub: Scrub) -> None:
        """Write through ``sessions``; ``scrub`` masks credentials in every stored text."""
        self._sessions = sessions
        self._scrub = scrub

    async def record(self, call: CallRecord) -> None:
        """Store ``call``; never raises for a storage or data problem (see the module text)."""
        try:
            prepared = self._prepare(call)
            async with self._sessions.begin() as session:
                await record_call(session, prepared)
        except (SQLAlchemyError, DataError, OSError, ValueError, TypeError) as exc:
            logger.warning(
                "call_log_write_failed",
                provider=call.provider.value,
                operation=call.operation,
                outcome=call.outcome.value,
                error_type=type(exc).__name__,
            )

    def _prepare(self, call: CallRecord) -> CallRecord:
        """``call`` with every text scrubbed first and then cut (a cut could split a secret)."""
        return replace(
            call,
            request=self._clean(call.request),
            response=None if call.response is None else self._clean(call.response),
            error=None if call.error is None else self._clean(call.error, MAX_LOGGED_ERROR),
        )

    def _clean(self, text: str, limit: int | None = None) -> str:
        scrubbed = self._scrub(text)
        return clip_text(scrubbed) if limit is None else clip_text(scrubbed, limit)
