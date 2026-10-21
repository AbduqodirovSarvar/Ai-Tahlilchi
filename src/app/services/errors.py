"""Errors raised by application services (contract #4, ``docs/contracts.md`` §4).

The bot maps each type to one user-facing message, so every error carries the structured
data the message needs (which field was wrong, how long to wait, how many requests are
missing) instead of a pre-formatted sentence.
"""

from datetime import timedelta

from app.errors import AppError


class ServiceError(AppError):
    """Base class of every expected failure a service reports to the bot."""


class ValidationFailedError(ServiceError, ValueError):
    """User input violates a rule of the contract; ``field`` names the offending input.

    Also a :class:`ValueError`, so generic parsing code can treat it as a bad value.
    """

    def __init__(self, field: str, message: str) -> None:
        """Keep the field name separately so the bot can re-ask for exactly that input."""
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


class NotFoundError(ServiceError, LookupError):
    """The referenced entity does not exist or does not belong to the user."""

    def __init__(self, entity: str, entity_id: object) -> None:
        """Remember what was looked up, for the log and the user message."""
        super().__init__(f"{entity} {entity_id!r} not found")
        self.entity = entity
        self.entity_id = entity_id


class CooldownActiveError(ServiceError):
    """A manual recalculation was requested again before the cooldown expired."""

    def __init__(self, retry_after: timedelta) -> None:
        """``retry_after`` is the remaining wait (always positive)."""
        super().__init__(f"cooldown active, retry after {retry_after}")
        self.retry_after = retry_after


class BudgetExceededError(ServiceError):
    """The request needs more provider API calls than the manual reserve has left."""

    def __init__(self, needed: int, available: int) -> None:
        """Both numbers are API requests; the bot suggests narrowing the query."""
        super().__init__(f"needs {needed} API requests, {available} available")
        self.needed = needed
        self.available = available
