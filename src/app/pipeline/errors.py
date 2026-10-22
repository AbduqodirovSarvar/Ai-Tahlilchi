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
