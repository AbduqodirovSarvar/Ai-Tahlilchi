"""Dead-man's-switch client (healthchecks.io): "the daily cycle ran" pings.

The monitoring service alerts when pings *stop* arriving, which catches the failure modes
nothing inside the process can report: a dead host, a stuck event loop, a crash loop. So the
scheduler pings after each successful daily job, and ``/fail`` when a job failed (an immediate
alert instead of waiting for the grace period).

A ping must never hurt the job it reports on: every network failure is logged and swallowed,
requests are bounded by a timeout and retried briefly. The URL is a secret (its UUID is the
credential), so it is never logged; only the outcome is.
"""

import asyncio
import re
from enum import StrEnum
from typing import Final, Self

import httpx

from app.config import Settings
from app.logging import get_logger
from app.tls import get_ssl_context

logger = get_logger(__name__)

PING_TIMEOUT_SECONDS: Final = 10.0
PING_ATTEMPTS: Final = 3
PING_BACKOFF_SECONDS: Final = 2.0
MAX_DETAIL_CHARS: Final = 40
"""The body is a short code (the job name), never free text: it goes to a third party."""
_CODE_PATTERN: Final = re.compile(r"[A-Za-z0-9_.-]+")


def short_code(detail: str | None) -> str:
    """The leading job-name-like token of ``detail``; internal error text never leaves the host."""
    match = _CODE_PATTERN.match((detail or "").strip())
    return "" if match is None else match.group(0)[:MAX_DETAIL_CHARS]


class PingKind(StrEnum):
    """Which healthchecks.io endpoint to call (the suffix appended to the check URL)."""

    SUCCESS = ""
    START = "/start"
    FAIL = "/fail"


class Heartbeat:
    """Pings one healthchecks.io check; a no-op when no URL is configured."""

    def __init__(
        self,
        url: str | None,
        *,
        attempts: int = PING_ATTEMPTS,
        backoff: float = PING_BACKOFF_SECONDS,
        timeout: float = PING_TIMEOUT_SECONDS,
    ) -> None:
        """Ping ``url`` (``None`` disables); ``attempts`` tries with doubling ``backoff``."""
        self._url = url.rstrip("/") if url else None
        self._attempts = max(1, attempts)
        self._backoff = backoff
        self._timeout = timeout

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """The heartbeat for ``HEALTHCHECKS_URL`` (disabled when it is not set)."""
        url = settings.healthchecks_url
        return cls(None if url is None else str(url))

    @property
    def enabled(self) -> bool:
        """Whether a check URL is configured."""
        return self._url is not None

    async def ping(self, kind: PingKind = PingKind.SUCCESS, detail: str | None = None) -> bool:
        """Send one ping; returns whether it was accepted. Never raises."""
        if self._url is None:
            return False
        target = self._url + kind.value
        body = short_code(detail).encode()
        for attempt in range(self._attempts):
            if attempt:
                await asyncio.sleep(self._backoff * 2 ** (attempt - 1))
            if await self._send(target, body, kind):
                return True
        logger.warning("heartbeat_failed", kind=kind.name.lower(), attempts=self._attempts)
        return False

    async def _send(self, target: str, body: bytes, kind: PingKind) -> bool:
        """One attempt; True when the service answered 2xx."""
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, verify=get_ssl_context() or True
            ) as client:
                response = await client.post(target, content=body)
        except (httpx.HTTPError, httpx.InvalidURL) as error:
            # The error text may embed the URL (a credential): log the type only.
            logger.info("heartbeat_error", kind=kind.name.lower(), error=type(error).__name__)
            return False
        if response.is_success:
            logger.info("heartbeat_sent", kind=kind.name.lower())
            return True
        logger.info("heartbeat_rejected", kind=kind.name.lower(), status=response.status_code)
        return False
