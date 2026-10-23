"""Scrubbing of free text (error messages) before it is stored or sent to anyone.

Errors from HTTP clients and libraries can embed URLs with API keys, bot tokens or bearer
headers. They are stored for ``/holat``, sent to the administrators and (as a short code only)
to third parties, so every such text passes :func:`scrub_text` first. Two layers: configured
secret values (registered once at start-up by ``main`` through :func:`register_secrets`) and
patterns of well-known credential shapes, which also catch secrets nobody registered.
"""

import re
from collections.abc import Iterable
from typing import Final

REDACTED: Final = "***"
MIN_SECRET_LENGTH: Final = 8
"""Shorter values would scrub ordinary words from every message (same rule as the log filter)."""
MAX_TEXT_LENGTH: Final = 500

_PATTERNS: Final = (
    re.compile(r"(?<!\d)\d{6,}:[A-Za-z0-9_-]{30,}"),  # Telegram bot token (urls: /bot<token>/)
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"),  # Google API key
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)([?&;\s](?:api[_-]?key|apikey|key|token|access_token|secret|password)=)[^&\s'\"]+"
    ),
    re.compile(r"(?i)((?:x-)?(?:apisports-key|api-key|authorization):\s*)\S+"),
)
_secret_values: tuple[str, ...] = ()


def register_secrets(values: Iterable[str]) -> None:
    """Remember the configured credential values to mask (replaces the previous set)."""
    global _secret_values  # noqa: PLW0603 - process-wide, set once at start-up
    _secret_values = tuple(
        sorted({v for v in values if len(v) >= MIN_SECRET_LENGTH}, key=len, reverse=True)
    )


def scrub_text(text: str, *, limit: int = MAX_TEXT_LENGTH) -> str:
    """``text`` with credentials masked, cut to ``limit`` characters."""
    for secret in _secret_values:
        text = text.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        text = pattern.sub(_mask, text)
    return text[:limit]


def _mask(match: re.Match[str]) -> str:
    """Keep the key name of ``key=value`` forms, drop the value."""
    return f"{match.group(1)}{REDACTED}" if match.re.groups else REDACTED
