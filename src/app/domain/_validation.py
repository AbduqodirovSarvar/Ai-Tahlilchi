"""Shared invariant checks for domain constructors (private to ``app.domain``)."""

import math
import re
from datetime import UTC, datetime
from typing import Final

from app.domain.errors import DomainValidationError

MAX_IDENTIFIER_LENGTH: Final = 255
MAX_NAME_LENGTH: Final = 255
_SLUG_PATTERN: Final = re.compile(r"[a-z0-9][a-z0-9_.-]*")


def require_utc(value: datetime, field: str) -> datetime:
    """Return ``value`` converted to UTC; naive datetimes are rejected, never guessed."""
    if value.tzinfo is None or value.utcoffset() is None:
        msg = f"{field} must be timezone-aware (got naive datetime {value.isoformat()})"
        raise DomainValidationError(msg)
    return value.astimezone(UTC)


def require_text(value: str, field: str, *, max_length: int = MAX_NAME_LENGTH) -> str:
    """Return ``value`` stripped; empty or over-long text is rejected."""
    stripped = value.strip()
    if not stripped:
        msg = f"{field} must not be empty"
        raise DomainValidationError(msg)
    if len(stripped) > max_length:
        msg = f"{field} must be at most {max_length} characters (got {len(stripped)})"
        raise DomainValidationError(msg)
    return stripped


def require_slug(value: str, field: str) -> str:
    """Return a lower-case machine identifier such as ``pinnacle`` or ``football-data``."""
    if len(value) > MAX_IDENTIFIER_LENGTH or not _SLUG_PATTERN.fullmatch(value):
        msg = f"{field} must be a lower-case slug like 'football-data' (got {value!r})"
        raise DomainValidationError(msg)
    return value


def require_finite(value: float, field: str) -> float:
    """Return ``value`` as float; NaN and infinities are rejected."""
    as_float = float(value)
    if not math.isfinite(as_float):
        msg = f"{field} must be a finite number (got {value!r})"
        raise DomainValidationError(msg)
    return as_float


def require_non_negative_int(value: int | None, field: str) -> int | None:
    """Return ``value`` unchanged if it is ``None`` or a non-negative integer."""
    if value is not None and value < 0:
        msg = f"{field} must be >= 0 (got {value})"
        raise DomainValidationError(msg)
    return value
