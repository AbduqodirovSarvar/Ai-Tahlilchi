"""Errors the pipeline reports to the service layer.

The services layer may not import ``app.clients`` (layering), so provider failures are
translated here: a spent quota becomes :class:`BudgetExhaustedError`, any other provider
failure :class:`ProviderUnavailableError`. The real services map them to the contract's
``BudgetExceededError`` / ``ServiceUnavailableError``.
"""

from app.errors import AppError


class PipelineError(AppError):
    """Base class of expected pipeline failures."""


class BudgetExhaustedError(PipelineError):
    """The provider quota cannot cover the requests an operation needs (nothing was sent)."""

    def __init__(self, needed: int, available: int) -> None:
        """Both numbers are provider requests."""
        super().__init__(f"needs {needed} API requests, {available} available")
        self.needed = needed
        self.available = available


class ProviderUnavailableError(PipelineError):
    """The data provider failed after its retries (network, 5xx, bad key or plan)."""

    def __init__(self, provider: str, detail: str) -> None:
        """``detail`` is the provider client's message; it never contains the API key."""
        super().__init__(f"{provider} unavailable: {detail}")
        self.provider = provider
        self.detail = detail


class DeliveryNotConfirmedError(PipelineError):
    """A message was handed to the notifier but nobody confirmed its delivery.

    The notifier answers ``None`` when the bot is not attached yet, the user blocked it or
    sending failed softly; the idempotency key must then stay open so the message is retried.
    """

    def __init__(self, what: str) -> None:
        """``what`` names the message, e.g. ``daily digest of user 7``."""
        super().__init__(f"{what} was not delivered")


class ManualResultError(PipelineError):
    """A hand-entered result is impossible (a cash-out beyond what the bet could ever pay)."""
