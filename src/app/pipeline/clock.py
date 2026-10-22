"""The injected "now": every pipeline job reads time through a :data:`Clock`, never the OS."""

from collections.abc import Callable
from datetime import UTC, datetime

type Clock = Callable[[], datetime]
"""Returns the current timezone-aware UTC instant; tests move it explicitly."""


def system_clock() -> datetime:
    """The real current UTC time (production wiring)."""
    return datetime.now(UTC)
