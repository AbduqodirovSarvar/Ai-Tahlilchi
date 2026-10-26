"""Log of the calls made to outside providers (API-Football, Gemini), for the admin to read.

A pure contract: the clients report each call to a :class:`CallRecorder`, the database layer
stores it, the bot shows it. Recording is best effort and must never break the call itself.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Protocol

MAX_LOGGED_TEXT: Final = 20_000
"""Longest request or response text kept per call; longer text is cut and marked."""


class Provider(StrEnum):
    """Who was called."""

    API_FOOTBALL = "api_football"
    GEMINI = "gemini"


class CallOutcome(StrEnum):
    """How a call ended."""

    OK = "ok"
    CACHED = "cached"  # answered from the local cache, no provider request was made
    ERROR = "error"
    QUOTA_BLOCKED = "quota_blocked"  # refused locally because the daily limit was reached


@dataclass(frozen=True, slots=True, kw_only=True)
class CallRecord:
    """One call to a provider.

    ``request`` holds the endpoint and query parameters (API-Football) or the prompt (Gemini);
    ``response`` holds the body or the model answer. Both are already scrubbed of secrets and
    cut to :data:`MAX_LOGGED_TEXT`.
    """

    at: datetime
    provider: Provider
    operation: str
    """API path such as ``fixtures`` / ``odds``, or the LLM task name."""
    outcome: CallOutcome
    duration_ms: int
    request: str
    response: str | None = None
    http_status: int | None = None
    quota_cost: int = 0
    """Requests this call used from the provider's daily limit (0 when cached or refused)."""
    size_bytes: int | None = None
    error: str | None = None


class CallRecorder(Protocol):
    """Where the clients report their calls."""

    async def record(self, call: CallRecord) -> None:
        """Store ``call``; implementations swallow and log their own failures."""
        ...


def clip_text(text: str, limit: int = MAX_LOGGED_TEXT) -> str:
    """``text`` cut to ``limit`` characters, with a visible marker when something was cut."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n…[{len(text) - limit} ta belgi qirqildi]"
